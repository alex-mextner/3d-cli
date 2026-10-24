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
import urllib.parse
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
    collect_pages,
    dict_list,
    str_list,
    unique_urls,
)

SITE = "cults"
API_URL = "https://cults3d.com/graphql"
WEB_URL = "https://cults3d.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?cults3d\.com/(?:[a-z]{2}/)?3d-model/[^/]+/([^/?#]+)", re.I)

PAGE_SIZE = 50  # creationsSearchBatch answers up to 100 per call
_FIELDS = ("identifier name(locale: EN) url(locale: EN) likesCount downloadsCount viewsCount "
           "publishedAt illustrationImageUrl illustrations { imageUrl } category { name } "
           "creator { nick shortUrl } license { name(locale: EN) } price(currency: EUR) { cents } "
           "description(locale: EN) tags(locale: EN) blueprints { fileName fileUrl }")
SEARCH_QUERY = ("query Search($query: String!, $limit: Int, $offset: Int) { result: creationsSearchBatch("
                f"query: $query, limit: $limit, offset: $offset) {{ total results {{ {_FIELDS} }} }} }}")
MODEL_QUERY = f"query Creation($slug: String!) {{ creation(slug: $slug) {{ {_FIELDS} }} }}"


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


def _files(item: dict[str, Any]) -> tuple[ModelFile, ...]:
    """One file per blueprint. A blueprint's `fileName` is listed even when its `fileUrl` is
    null (paid creations, or any answer that does not hand out the link); the file name falls
    back to the URL's last segment only for answers that carry no fileName."""
    files = []
    for i, blueprint in enumerate(dict_list(item.get("blueprints")), start=1):
        url = as_str(blueprint.get("fileUrl"))
        name = as_str(blueprint.get("fileName")) or os.path.basename(
            urllib.parse.unquote(url.split("?", 1)[0])) or f"file-{i}"
        files.append(ModelFile(name=name, ref=url, kind="blueprint"))
    return tuple(files)


def _model(item: dict[str, Any]) -> Model:
    creator = child(item, "creator")
    lic = child(item, "license")
    # Free only when Cults says 0 cents; a missing or unreadable price counts as paid, so a
    # schema change can never turn a priced creation into a "free" download.
    cents = as_int(child(item, "price").get("cents"))
    price = "free" if cents == 0 else (f"{cents / 100:.2f} EUR" if cents else "price unknown")
    cover = as_str(item.get("illustrationImageUrl"))
    illustrations = tuple(as_str(i.get("imageUrl")) for i in dict_list(item.get("illustrations")))
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
        summary=" ".join(as_str(item.get("description")).split())[:500],
        tags=str_list(item.get("tags")),
        image=cover,
        files=_files(item),
        date=as_str(item.get("publishedAt")),
        images=unique_urls(cover, *illustrations),
        views=as_int(item.get("viewsCount")),
        category=as_str(child(item, "category").get("name")),
        files_known=isinstance(item.get("blueprints"), list),
    )


def parse_search(data: dict[str, Any]) -> list[Model]:
    result = child(data, "result")
    return [_model(item) for item in dict_list(result.get("results"))]


def parse_model(data: dict[str, Any]) -> Model:
    item = data.get("creation")
    if not isinstance(item, dict):
        raise MarketplaceError(SITE, "not-found", "creation not found")
    return _model(item)


def search(query: str, limit: int) -> list[Model]:
    def page(offset: int, size: int) -> list[Model]:
        return parse_search(_graphql(SEARCH_QUERY, {"query": query, "limit": size, "offset": offset}))

    return collect_pages(page, limit, PAGE_SIZE)


def show(ref: str) -> Model:
    return parse_model(_graphql(MODEL_QUERY, {"slug": ref}))


def download_link(model: Model, file: ModelFile) -> tuple[str, dict[str, str]]:
    """The file's URL from the listing, which Cults only fills in for downloadable files."""
    if not file.ref:
        raise MarketplaceError(SITE, "http", f"no file URL for {file.name} of {model.url}")
    return file.ref, {}


def download_links(model: Model) -> DownloadLinks:
    missing = [f.name for f in model.files if not f.ref]
    if missing:
        raise MarketplaceError(SITE, "http", f"no file URL for {', '.join(missing)}")
    return [(f, f.ref, {}) for f in model.files]
