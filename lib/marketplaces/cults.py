"""Cults3D through its official GraphQL API (https://cults3d.com/graphql).

The website answers every non-browser client, TLS-impersonating ones included, with a
Cloudflare Turnstile challenge, so the API is the only route; it requires HTTP Basic auth
with the user's Cults nick and an API key (credentials.cults_basic_auth). Cults is mostly a
paid marketplace: search reports the EUR price, and download refuses any priced creation.
Field names follow Cults' published examples (creationsSearchBatch, creation(slug),
blueprints { fileUrl }); the schema itself is only visible to an authenticated client.
"""
from __future__ import annotations

import base64
import os
import re
from typing import Any

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
    str_list,
)

SITE = "cults"
API_URL = "https://cults3d.com/graphql"
WEB_URL = "https://cults3d.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?cults3d\.com/(?:[a-z]{2}/)?3d-model/[^/]+/([^/?#]+)", re.I)

_FIELDS = ("identifier name(locale: EN) url(locale: EN) likesCount downloadsCount "
           "illustrationImageUrl creator { nick shortUrl } license { name(locale: EN) } "
           "price(currency: EUR) { cents }")
SEARCH_QUERY = ("query Search($query: String!, $limit: Int) { result: creationsSearchBatch("
                f"query: $query, limit: $limit) {{ total results {{ {_FIELDS} }} }} }}")
MODEL_QUERY = (f"query Creation($slug: String!) {{ creation(slug: $slug) {{ {_FIELDS} "
               "tags(locale: EN) blueprints { fileUrl } } }")


def api_url() -> str:
    return os.environ.get("THREED_CULTS_API", API_URL)


def match(url: str) -> str | None:
    found = _URL_RE.match(url.strip())
    return found.group(1) if found else None


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    username, api_key = credentials.cults_basic_auth()
    basic = base64.b64encode(f"{username}:{api_key}".encode()).decode("ascii")
    return transport.graphql(SITE, api_url(), query, variables,
                             headers={"Authorization": f"Basic {basic}"},
                             auth_hint=("Cults rejected the username/API key; check them at "
                                        f"{credentials.CULTS_KEYS_URL}."))


def _model(item: dict[str, Any], *, files: tuple[ModelFile, ...] = ()) -> Model:
    creator = child(item, "creator")
    lic = child(item, "license")
    # Free only when Cults says 0 cents; a missing or unreadable price counts as paid, so a
    # schema change can never turn a priced creation into a "free" download.
    cents = as_int(child(item, "price").get("cents"))
    price = "free" if cents == 0 else (f"{cents / 100:.2f} EUR" if cents else "price unknown")
    return Model(
        site=SITE,
        id=as_str(item.get("identifier")),
        title=as_str(item.get("name")),
        url=as_str(item.get("url")),
        author=as_str(creator.get("nick")),
        author_url=as_str(creator.get("shortUrl")),
        likes=as_int(item.get("likesCount")),
        downloads=as_int(item.get("downloadsCount")),
        license=as_str(lic.get("name")),
        price=price,
        paid=cents != 0,
        tags=str_list(item.get("tags")),
        image=as_str(item.get("illustrationImageUrl")),
        files=files,
    )


def parse_search(data: dict[str, Any]) -> list[Model]:
    result = child(data, "result")
    return [_model(item) for item in dict_list(result.get("results"))]


def parse_model(data: dict[str, Any]) -> Model:
    item = data.get("creation")
    if not isinstance(item, dict):
        raise MarketplaceError(SITE, "not-found", "creation not found")
    files = tuple(
        ModelFile(name=os.path.basename(as_str(b.get("fileUrl")).split("?", 1)[0]) or f"file-{i}",
                  ref=as_str(b.get("fileUrl")), kind="blueprint")
        for i, b in enumerate(dict_list(item.get("blueprints")), start=1)
    )
    return _model(item, files=files)


def search(query: str, limit: int) -> list[Model]:
    return parse_search(_graphql(SEARCH_QUERY, {"query": query, "limit": limit}))[:limit]


def show(ref: str) -> Model:
    return parse_model(_graphql(MODEL_QUERY, {"slug": ref}))


def download_links(model: Model) -> DownloadLinks:
    missing = [f.name for f in model.files if not f.ref]
    if missing:
        raise MarketplaceError(SITE, "http", f"no file URL for {', '.join(missing)}")
    return [(f, f.ref, {}) for f in model.files]
