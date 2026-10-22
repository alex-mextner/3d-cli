"""Printables (Prusa) through its public GraphQL API at api.printables.com.

The www.printables.com pages sit behind a Cloudflare challenge, but the API host does not
and needs no login for free models: `searchPrints2` searches, `print(id)` returns the
metadata and file lists, and the `getDownloadLink` mutation hands out a signed CDN link per
file (files.printables.com, valid 24 h). Introspection is disabled, so the queries below are
the ones the website itself sends. Store (paid) and Club (premium) models are refused.
"""
from __future__ import annotations

import os
import re
from typing import Any

from marketplaces import transport
from marketplaces.base import (
    DownloadLinks,
    MarketplaceError,
    Model,
    ModelFile,
    as_int,
    as_str,
    child,
    dict_list,
)

SITE = "printables"
API_URL = "https://api.printables.com/graphql/"
WEB_URL = "https://www.printables.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?printables\.com/(?:[a-z]{2}/)?model/(\d+)", re.I)
# getDownloadLink's DownloadFileTypeEnum value for each file list of a print.
FILE_LISTS = {"stls": "stl", "gcodes": "gcode", "slas": "sla", "otherFiles": "other"}

_MODEL_FIELDS = """id name slug likesCount downloadCount premium price
  user { handle publicUsername } license { name abbreviation } image { filePath }"""
SEARCH_QUERY = (
    "query SearchModels($query: String!, $limit: Int) { result: searchPrints2(query: $query, "
    f"printType: print, limit: $limit, ordering: best_match) {{ items {{ {_MODEL_FIELDS} }} }} }}"
)
_FILE_FIELDS = "id name fileSize"
MODEL_QUERY = (
    "query ModelDetail($id: ID!) { print(id: $id) { "
    f"{_MODEL_FIELDS} summary tags {{ name }} "
    + " ".join(f"{key} {{ {_FILE_FIELDS} }}" for key in FILE_LISTS)
    + " } }"
)
DOWNLOAD_MUTATION = (
    "mutation GetDownloadLink($id: ID!, $printId: ID!, $fileType: DownloadFileTypeEnum!, "
    "$source: DownloadSourceEnum!) { getDownloadLink(id: $id, printId: $printId, "
    "fileType: $fileType, source: $source) { ok errors { field messages } output { link } } }"
)


def api_url() -> str:
    return os.environ.get("THREED_PRINTABLES_API", API_URL)


def match(url: str) -> str | None:
    found = _URL_RE.match(url.strip())
    return found.group(1) if found else None


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    return transport.graphql(SITE, api_url(), query, variables, headers={"Origin": WEB_URL})


def _model(item: dict[str, Any], *, files: tuple[ModelFile, ...] = ()) -> Model:
    model_id = str(item.get("id") or "")
    slug = as_str(item.get("slug"))
    user = child(item, "user")
    lic = child(item, "license")
    image = child(item, "image")
    price = item.get("price")
    paid = bool(item.get("premium")) or bool(price)
    handle = as_str(user.get("handle"))
    return Model(
        site=SITE,
        id=model_id,
        title=as_str(item.get("name")),
        url=f"{WEB_URL}/model/{model_id}-{slug}" if slug else f"{WEB_URL}/model/{model_id}",
        author=as_str(user.get("publicUsername")) or handle,
        author_url=f"{WEB_URL}/@{handle}" if handle else "",
        likes=as_int(item.get("likesCount")),
        downloads=as_int(item.get("downloadCount")),
        license=as_str(lic.get("abbreviation")) or as_str(lic.get("name")),
        price=f"{price}" if price else ("Printables Club" if item.get("premium") else ""),
        paid=paid,
        summary=as_str(item.get("summary"))[:500],
        tags=tuple(as_str(t.get("name")) for t in dict_list(item.get("tags")) if t.get("name")),
        image=f"https://media.printables.com/{as_str(image.get('filePath'))}"
        if image.get("filePath") else "",
        files=files,
    )


def parse_search(data: dict[str, Any]) -> list[Model]:
    result = child(data, "result")
    return [_model(item) for item in dict_list(result.get("items"))]


def parse_model(data: dict[str, Any]) -> Model:
    item = data.get("print")
    if not isinstance(item, dict):
        raise MarketplaceError(SITE, "not-found", "model not found")
    files = tuple(
        ModelFile(name=as_str(f.get("name")), size=as_int(f.get("fileSize")),
                  ref=str(f.get("id") or ""), kind=kind)
        for key, kind in FILE_LISTS.items()
        for f in dict_list(item.get(key))
    )
    return _model(item, files=files)


def search(query: str, limit: int) -> list[Model]:
    return parse_search(_graphql(SEARCH_QUERY, {"query": query, "limit": limit}))[:limit]


def show(ref: str) -> Model:
    return parse_model(_graphql(MODEL_QUERY, {"id": ref}))


def parse_download_link(data: dict[str, Any], file: ModelFile) -> str:
    result = child(data, "getDownloadLink")
    output = child(result, "output")
    link = as_str(output.get("link"))
    if not (result.get("ok") and link):
        messages = "; ".join(" ".join(filter(None, map(as_str, e["messages"])))
                             for e in dict_list(result.get("errors")) if isinstance(e.get("messages"), list))
        raise MarketplaceError(SITE, "http", f"no download link for {file.name}"
                               + (f": {messages}" if messages else ""))
    return link


def download_links(model: Model) -> DownloadLinks:
    links: DownloadLinks = []
    for file in model.files:
        data = _graphql(DOWNLOAD_MUTATION, {"id": file.ref, "printId": model.id,
                                            "fileType": file.kind, "source": "model_detail"})
        links.append((file, parse_download_link(data, file), {}))
    return links
