"""Thingiverse through its REST API (api.thingiverse.com), authenticated with the user's own
App Token (see credentials.py) — the website is a JavaScript app with no data in its HTML,
and the API answers 401 NO_TOKEN_PROVIDED without a token. Search hits carry no license, so
search fills it from one GET /things/{id} per hit (the API allows 300 requests / 5 min); a
hit whose detail call fails keeps an empty license rather than sinking the whole search.
Files come from GET /things/{id}/files: `direct_url` is a public CDN link, `download_url`
goes through the API and needs the token.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from cli.resources import pool_size
from marketplaces import credentials, transport
from marketplaces.base import (
    DownloadLinks,
    MarketplaceError,
    Model,
    ModelFile,
    as_int,
    as_str,
    child,
    dict_list,
    require_dict,
)

SITE = "thingiverse"
API_URL = "https://api.thingiverse.com"
WEB_URL = "https://www.thingiverse.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?thingiverse\.com/thing:(\d+)", re.I)
_DETAIL_WORKERS = 4  # polite cap on parallel detail calls; THREED_MAX_WORKERS can lower it


def api_url() -> str:
    return os.environ.get("THREED_THINGIVERSE_API", API_URL).rstrip("/")


def match(url: str) -> str | None:
    found = _URL_RE.match(url.strip())
    return found.group(1) if found else None


def _auth_hint() -> str:
    return (f"Check the Thingiverse App Token (create one at {credentials.THINGIVERSE_APPS_URL}); "
            "it goes in auth.json `thingiverse.token` or THINGIVERSE_TOKEN.")


def _get(path: str, params: dict[str, str] | None = None) -> Any:
    token = credentials.thingiverse_token()
    url = f"{api_url()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    response = transport.fetch(SITE, url, headers={"Authorization": f"Bearer {token}"})
    transport.raise_for_status(SITE, response, auth_hint=_auth_hint())
    return transport.decode_json(SITE, response)


def _model(thing: dict[str, Any], *, files: tuple[ModelFile, ...] = ()) -> Model:
    creator = child(thing, "creator")
    thing_id = str(thing.get("id") or "")
    tags = tuple(as_str(t.get("name")) for t in dict_list(thing.get("tags")) if t.get("name"))
    return Model(
        site=SITE,
        id=thing_id,
        title=as_str(thing.get("name")),
        url=as_str(thing.get("public_url")) or f"{WEB_URL}/thing:{thing_id}",
        author=as_str(creator.get("name")),
        author_url=as_str(creator.get("public_url")),
        likes=as_int(thing.get("like_count")),
        downloads=as_int(thing.get("download_count")),
        license=as_str(thing.get("license")),
        summary=as_str(thing.get("description"))[:500],
        tags=tags,
        image=as_str(thing.get("thumbnail")),
        files=files,
    )


def parse_search(payload: Any) -> list[Model]:
    """GET /search/{term}?type=things → {"total": N, "hits": [thing, ...]} (older API
    versions answered with a bare list)."""
    hits = payload.get("hits") if isinstance(payload, dict) else payload
    return [_model(thing) for thing in dict_list(hits)]


def parse_files(payload: Any) -> tuple[ModelFile, ...]:
    files = []
    for entry in dict_list(payload):
        url = as_str(entry.get("direct_url")) or as_str(entry.get("download_url"))
        files.append(ModelFile(name=as_str(entry.get("name")), size=as_int(entry.get("size")),
                               ref=url, kind="file"))
    return tuple(files)


def parse_model(thing: Any, files: Any) -> Model:
    return _model(require_dict(SITE, thing, "thing"), files=parse_files(files))


def _with_detail(model: Model) -> Model:
    """Fill license (and downloads when absent) from GET /things/{id}; on any site error the
    hit stays as it was (a deleted thing, a 429)."""
    try:
        thing = _get(f"/things/{model.id}")
    except MarketplaceError:
        return model
    if not isinstance(thing, dict):
        return model
    downloads = model.downloads if model.downloads is not None else as_int(thing.get("download_count"))
    return replace(model, license=as_str(thing.get("license")), downloads=downloads)


def search(query: str, limit: int) -> list[Model]:
    payload = _get(f"/search/{urllib.parse.quote(query, safe='')}",
                   {"type": "things", "per_page": str(limit), "page": "1", "sort": "relevant"})
    models = parse_search(payload)[:limit]
    with ThreadPoolExecutor(max_workers=min(_DETAIL_WORKERS, pool_size(len(models)))) as pool:
        return list(pool.map(lambda m: m if m.license else _with_detail(m), models))


def show(ref: str) -> Model:
    return parse_model(_get(f"/things/{ref}"), _get(f"/things/{ref}/files"))


def download_links(model: Model) -> DownloadLinks:
    links: DownloadLinks = []
    for file in model.files:
        if not file.ref:
            raise MarketplaceError(SITE, "http", f"no download URL for {file.name}")
        headers: dict[str, str] = {}
        if urllib.parse.urlsplit(file.ref).netloc == urllib.parse.urlsplit(api_url()).netloc:
            headers["Authorization"] = f"Bearer {credentials.thingiverse_token()}"
        links.append((file, file.ref, headers))
    return links
