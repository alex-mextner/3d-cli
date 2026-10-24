"""Thingiverse through its REST API (api.thingiverse.com), authenticated with the user's own
App Token (see credentials.py) — the website is a JavaScript app with no data in its HTML,
and the API answers 401 NO_TOKEN_PROVIDED without a token. A search costs one request per
page; its hits carry no license and no file list, so `enrich` adds both from GET /things/{id}
and GET /things/{id}/files, at most one of each per hit, with a capped number of requests in
flight, a wall-clock budget (THREED_THINGIVERSE_BUDGET_S, default 30 s) and a short-lived cache
(the API allows 300 requests / 5 min). A hit whose follow-up calls fail or miss the budget
keeps what the search gave it (`files_known` stays False) rather than sinking the search.
Files come from GET /things/{id}/files: `direct_url` is a public CDN link, `download_url`
goes through the API and needs the token.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from cli.resources import pool_size
from marketplaces import credentials, transport
from marketplaces.base import (
    Deadline,
    DetailStore,
    DownloadLinks,
    MarketplaceError,
    Model,
    ModelFile,
    as_int,
    as_str,
    child,
    dict_list,
    require_dict,
    unique_urls,
)

SITE = "thingiverse"
API_URL = "https://api.thingiverse.com"
WEB_URL = "https://www.thingiverse.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?thingiverse\.com/thing:(\d+)", re.I)
PAGE_SIZE = 50
_DETAIL_WORKERS = 4  # polite cap on parallel follow-up calls; THREED_MAX_WORKERS can lower it
DEFAULT_BUDGET_S = 30.0
_CACHE_TTL_S = 600.0
_CACHE_MAX = 1024
# thing id -> (monotonic time stored, detail answer, file list); guarded by _CACHE_LOCK.
_CACHE: dict[str, tuple[float, dict[str, Any], tuple[ModelFile, ...]]] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def api_url() -> str:
    return os.environ.get("THREED_THINGIVERSE_API", API_URL).rstrip("/")


def budget_seconds() -> float:
    raw = os.environ.get("THREED_THINGIVERSE_BUDGET_S", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_S
    return value if value >= 0 and math.isfinite(value) else DEFAULT_BUDGET_S


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


def _tags(thing: dict[str, Any]) -> tuple[str, ...]:
    return tuple(as_str(t.get("name")) for t in dict_list(thing.get("tags")) if t.get("name"))


def _model(thing: dict[str, Any], *, files: tuple[ModelFile, ...] | None = None) -> Model:
    creator = child(thing, "creator")
    thing_id = str(thing.get("id") or "")
    thumbnail = as_str(thing.get("thumbnail"))
    preview = as_str(thing.get("preview_image"))
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
        tags=_tags(thing),
        image=preview or thumbnail,
        files=files or (),
        date=as_str(thing.get("added")),
        images=unique_urls(preview, thumbnail),
        views=as_int(thing.get("view_count")),
        files_known=files is not None,
    )


def parse_search_page(payload: Any) -> tuple[list[Model], int | None]:
    """GET /search/{term}?type=things → {"total": N, "hits": [thing, ...]} (older API
    versions answered with a bare list, which has no total)."""
    hits = payload.get("hits") if isinstance(payload, dict) else payload
    total = as_int(payload.get("total")) if isinstance(payload, dict) else None
    return [_model(thing) for thing in dict_list(hits)], total


def parse_search(payload: Any) -> list[Model]:
    return parse_search_page(payload)[0]


def parse_files(payload: Any) -> tuple[ModelFile, ...]:
    files = []
    for entry in dict_list(payload):
        url = as_str(entry.get("direct_url")) or as_str(entry.get("download_url"))
        files.append(ModelFile(name=as_str(entry.get("name")), size=as_int(entry.get("size")),
                               ref=url, kind="file"))
    return tuple(files)


def parse_model(thing: Any, files: Any) -> Model:
    return _model(require_dict(SITE, thing, "thing"), files=parse_files(files))


def search(query: str, limit: int) -> list[Model]:
    """One request per page of PAGE_SIZE hits (a short page does not end the search: the API
    may cap per_page below what was asked), until `limit` hits, an empty page or `total`."""
    models: list[Model] = []
    seen: set[str] = set()
    for page in range(1, -(-limit // PAGE_SIZE) + 3):
        payload = _get(f"/search/{urllib.parse.quote(query, safe='')}",
                       {"type": "things", "per_page": str(min(PAGE_SIZE, limit - len(models))),
                        "page": str(page), "sort": "relevant"})
        hits, total = parse_search_page(payload)
        fresh = [m for m in hits if m.id not in seen]
        seen.update(m.id for m in hits)
        models.extend(fresh)
        if len(models) >= limit or not hits or (total is not None and len(seen) >= total):
            break
    return models[:limit]


def _cached(thing_id: str) -> tuple[dict[str, Any], tuple[ModelFile, ...]] | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(thing_id)
        if hit is not None and time.monotonic() - hit[0] <= _CACHE_TTL_S:
            return hit[1], hit[2]
        _CACHE.pop(thing_id, None)
    return None


def _remember(thing_id: str, thing: dict[str, Any], files: tuple[ModelFile, ...]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[thing_id] = (time.monotonic(), thing, files)


def _apply(model: Model, thing: dict[str, Any], files: tuple[ModelFile, ...]) -> Model:
    return replace(
        model,
        license=as_str(thing.get("license")) or model.license,
        downloads=model.downloads if model.downloads is not None else as_int(thing.get("download_count")),
        views=model.views if model.views is not None else as_int(thing.get("view_count")),
        summary=model.summary or as_str(thing.get("description"))[:500],
        tags=model.tags or _tags(thing),
        files=files,
        files_known=True,
    )


_DETAIL_KEYS = ("license", "download_count", "view_count", "description", "tags")


def _stored(store: DetailStore | None, thing_id: str) -> tuple[dict[str, Any], tuple[ModelFile, ...]] | None:
    """The answer a previous process stored for `thing_id`, decoded; None when absent or malformed."""
    raw = store.get(f"thingiverse:{thing_id}") if store is not None else None
    if not isinstance(raw, dict) or not isinstance(raw.get("thing"), dict):
        return None
    files = tuple(ModelFile(name=as_str(f.get("name")), size=as_int(f.get("size")), ref=as_str(f.get("ref")),
                            kind="file") for f in dict_list(raw.get("files")))
    return raw["thing"], files


def _enrich_one(model: Model, deadline: Deadline, store: DetailStore | None) -> Model:
    """License/tags from GET /things/{id} and the file list from GET /things/{id}/files; the
    hit stays as it was on any site error (a deleted thing, a 429) or once the budget is gone.
    Answers come from the in-process cache, then from `store`, before any request is made."""
    cached = _cached(model.id) or _stored(store, model.id)
    if cached is not None:
        _remember(model.id, *cached)
        return _apply(model, *cached)
    if deadline.expired():
        return model
    try:
        thing = _get(f"/things/{model.id}")
        if not isinstance(thing, dict) or deadline.expired():
            return model
        files = parse_files(_get(f"/things/{model.id}/files"))
    except MarketplaceError:
        return model
    slim = {k: thing[k] for k in _DETAIL_KEYS if k in thing}
    _remember(model.id, slim, files)
    if store is not None:
        store.put(f"thingiverse:{model.id}", {"thing": slim, "files": [
            {"name": f.name, "size": f.size, "ref": f.ref} for f in files]})
    return _apply(model, slim, files)


def enrich(models: list[Model], store: DetailStore | None = None, *, budget_s: float | None = None) -> list[Model]:
    """Add license and file lists to search hits: at most two follow-up calls per hit, at most
    `_DETAIL_WORKERS` in flight, none started after the time budget is spent. `store` (optional) is the
    persistent cache consulted before, and filled after, every request."""
    if not models:
        return models
    deadline = Deadline(budget_seconds() if budget_s is None else budget_s)
    with ThreadPoolExecutor(max_workers=min(_DETAIL_WORKERS, pool_size(len(models)))) as pool:
        return list(pool.map(lambda m: _enrich_one(m, deadline, store), models))


def show(ref: str) -> Model:
    return parse_model(_get(f"/things/{ref}"), _get(f"/things/{ref}/files"))


def download_link(model: Model, file: ModelFile) -> tuple[str, dict[str, str]]:
    if not file.ref:
        raise MarketplaceError(SITE, "http", f"no download URL for {file.name} of {model.url}")
    headers: dict[str, str] = {}
    if transport.same_host(file.ref, api_url()):
        headers["Authorization"] = f"Bearer {credentials.thingiverse_token()}"
    return file.ref, headers


def download_links(model: Model) -> DownloadLinks:
    links: DownloadLinks = []
    for file in model.files:
        url, headers = download_link(model, file)
        links.append((file, url, headers))
    return links
