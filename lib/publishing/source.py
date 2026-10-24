"""The source of a remix or make: what `--of SOURCE_URL_OR_MODEL_JSON` names.

`--of` takes a model URL, a `model.json` written by `3d models download`, or the directory holding one. The
licence is read where it was recorded: a `model.json` carries the licence the marketplace listed at download
time; a URL that the library knows as a `model_source` carries the licence S5's download stored. Only when
neither says anything is the marketplace asked (the existing `remix.marketplace_license`, network).

Stdlib only; the marketplace URL matchers are imported lazily.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def split_url(url: str) -> tuple[str, str]:
    """(site, model id) of a marketplace URL, or ("", "") for an unsupported one. No network."""
    from marketplaces import catalog

    for client in catalog.CLIENTS.values():
        ref = client.match(url)
        if ref:
            return client.site, str(ref)
    return "", ""


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
    if not isinstance(src, dict) or not url.startswith(("http://", "https://")):
        raise InvalidArgument(
            "--of",
            str(path),
            ["a model.json with source.url (re-download the model to get one)"],
            command="models",
        )
    author = data.get("author")
    site, model_id = str(src.get("site", "")), str(src.get("id", ""))
    if not site or not model_id:
        site, model_id = split_url(url)
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
    site, model_id = split_url(of)
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
