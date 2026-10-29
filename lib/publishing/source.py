"""The source of a remix or make: what `--of SOURCE_URL_OR_MODEL_JSON` names.

`--of` takes a model URL, a `model.json` written by `3d models download`, or the directory holding one. The
licence is read where it was recorded: a `model.json` carries the licence the marketplace listed at download
time; a URL that the library knows as a `model_source` carries the licence S5's download stored. Only when
neither says anything is the marketplace asked (the existing `remix.marketplace_license`, network).

Stdlib only. The marketplace URL routes are matched strictly here (see `split_url`), not by the loose
`marketplaces.*.match` prefix matchers: a source URL is later opened by a browser.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from errors import InputNotFound, InvalidArgument, ThreeDError
from publishing.remix import LicenseLookup, marketplace_license


@dataclass(frozen=True)
class SourceRef:
    url: str
    site: str  # "" when the URL belongs to no supported marketplace
    model_id: str
    license: str  # the licence text recorded for it ("" = unknown)
    author: str
    title: str
    origin: str  # model.json | library | url

    def to_json(self) -> dict[str, str]:
        return {
            "url": self.url,
            "site": self.site,
            "model_id": self.model_id,
            "license": self.license,
            "author": self.author,
            "title": self.title,
            "origin": self.origin,
        }


_SLUG = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# Exact model-page route of each marketplace: (hostnames, path pattern whose group 1 is the model id). A route
# has no query, fragment or sub-path, so anything else is not "the model page" the browser flows would open.
_MODEL_PAGES: dict[str, tuple[frozenset[str], re.Pattern[str]]] = {
    "printables": (
        frozenset({"printables.com", "www.printables.com"}),
        re.compile(rf"/(?:[a-z]{{2}}/)?model/(\d+)(?:-{_SLUG})?/?"),
    ),
    "makerworld": (
        frozenset({"makerworld.com", "www.makerworld.com"}),
        re.compile(rf"/(?:[a-z]{{2}}(?:-[a-z]{{2}})?/)?models/(\d+)(?:-{_SLUG})?/?"),
    ),
    "thingiverse": (
        frozenset({"thingiverse.com", "www.thingiverse.com"}),
        re.compile(r"/thing:(\d+)/?"),
    ),
    "cults": (
        frozenset({"cults3d.com", "www.cults3d.com"}),
        re.compile(rf"/(?:[a-z]{{2}}/)?3d-model/{_SLUG}/({_SLUG})/?"),
    ),
}
_UNSAFE_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


def split_url(url: str) -> tuple[str, str]:
    """(site, model id) of a canonical marketplace model-page URL, or ("", "") for anything else. No network.

    Canonical means `https://[www.]HOST/<the site's model route>` and nothing more: no userinfo or port, no
    query or fragment, no `.`/`..`/percent-encoded segments, no sub-path. A URL that merely starts like a model
    page (`.../model/311253/../../model/999`) names another page once a browser normalises it, so the id read
    from the path must be the id of the page that is opened."""
    if _UNSAFE_URL_CHARS.search(url):
        return "", ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    if (
        parts.scheme != "https"
        or parts.query
        or parts.fragment
        or "?" in url
        or "#" in url
    ):
        return "", ""
    host = parts.netloc.lower()
    for site, (hosts, route) in _MODEL_PAGES.items():
        found = route.fullmatch(parts.path) if host in hosts else None
        if found:
            return site, found.group(1)
    return "", ""


def is_marketplace_host(url: str) -> bool:
    """Whether `url` names a supported marketplace domain anywhere (decides if a non-canonical URL is a
    malformed model page rather than an unrelated link). Deliberately a substring test: lookalike hosts and
    `host\\@evil` backslash tricks must not slip through as "some other site"."""
    lowered = url.lower()
    return any(
        h.removeprefix("www.") in lowered
        for hosts, _ in _MODEL_PAGES.values()
        for h in hosts
    )


def require_model_page(url: str, shown: str) -> tuple[str, str]:
    """`split_url`, but a URL that mentions a marketplace host and is not that site's exact model page is
    refused (InvalidArgument, exit 2) instead of passing as an unknown site. Other URLs give ("", "")."""
    site, model_id = split_url(url)
    if not site and is_marketplace_host(url):
        raise InvalidArgument(
            "--of",
            shown,
            [
                "the plain model page of a marketplace model, e.g. "
                "https://www.printables.com/model/311253-spoon-drawer-pull "
                f"(no query, fragment, extra path or `..`; got {url!r})"
            ],
            command="models",
        )
    return site, model_id


def _checked_identity(path: Path, src: dict[str, Any], url: str) -> tuple[str, str]:
    """(site, id) of a model.json source, derived from its URL. A declared site/id that the URL does not
    back is forged or corrupt: the make/remix flows would open that URL, so it must be a marketplace page
    of the declared model."""
    site, model_id = require_model_page(url, str(path))
    declared_site, declared_id = str(src.get("site", "")), str(src.get("id", ""))
    if (declared_site and declared_site != site) or (
        declared_id and declared_id != model_id
    ):
        raise InvalidArgument(
            "--of",
            str(path),
            [
                f"a model.json whose source.url ({url}) is the {declared_site or 'declared'} model page "
                f"{declared_id or ''}".rstrip()
            ],
            command="models",
        )
    return site, model_id


def _from_json(path: Path) -> SourceRef:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidArgument(
            "--of",
            str(path),
            ["a model.json written by `3d models download`"],
            command="models",
        ) from exc
    src = data.get("source") if isinstance(data, dict) else None
    url = str(src.get("url", "")) if isinstance(src, dict) else ""
    if not isinstance(src, dict) or not url.startswith("https://"):
        raise InvalidArgument(
            "--of",
            str(path),
            ["a model.json with source.url (re-download the model to get one)"],
            command="models",
        )
    author = data.get("author")
    site, model_id = _checked_identity(path, src, url)
    return SourceRef(
        url=url,
        site=site,
        model_id=model_id,
        license=str(data.get("license") or ""),
        author=str(author.get("name", ""))
        if isinstance(author, dict)
        else str(author or ""),
        title=str(data.get("title") or ""),
        origin="model.json",
    )


def load_source(of: str, conn: sqlite3.Connection | None = None) -> SourceRef:
    """Resolve `--of`. A path (file or directory with model.json) wins over a URL; a URL is looked up in the
    library's `model_source` table (`conn`) for its recorded licence."""
    path = Path(of).expanduser()
    if path.is_dir() and (path / "model.json").is_file():
        return _from_json(path / "model.json")
    if path.is_file():
        return _from_json(path)
    if not of.startswith(("http://", "https://")):
        raise InputNotFound(of, command="models")
    site, model_id = require_model_page(of, of)
    if conn is not None and site:
        row = conn.execute(
            "SELECT url, license, author FROM model_source WHERE site = ? AND model_id = ?",
            (site, model_id),
        ).fetchone()
        if row is not None:
            return SourceRef(
                of,
                site,
                model_id,
                str(row["license"] or ""),
                str(row["author"] or ""),
                "",
                "library",
            )
    return SourceRef(of, site, model_id, "", "", "", "url")


def lookup_for(source: SourceRef | None) -> LicenseLookup:
    """The licence lookup `remix.check_remix` calls: the recorded licence of `source` when it has one, else the
    marketplace (network)."""

    def lookup(url: str) -> str:
        if source is not None and source.url == url and source.license:
            return source.license
        return marketplace_license(url)

    return lookup


def site_of(url: str) -> str:
    """The marketplace a URL belongs to ("" when none) — decides where a make of that model is posted."""
    try:
        return split_url(url)[0]
    except ThreeDError:
        return ""
