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
    collect_pages,
    dict_list,
    unique_urls,
)

SITE = "printables"
API_URL = "https://api.printables.com/graphql/"
WEB_URL = "https://www.printables.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?printables\.com/(?:[a-z]{2}/)?model/(\d+)", re.I)
# getDownloadLink's DownloadFileTypeEnum value for each file list of a print.
FILE_LISTS = {"stls": "stl", "gcodes": "gcode", "slas": "sla", "otherFiles": "other"}

_FILE_FIELDS = "id name fileSize"
_FILE_LISTS = " ".join(f"{key} {{ {_FILE_FIELDS} }}" for key in FILE_LISTS)
# One card per model: search hits and the detail answer carry the same fields, file lists included
# (a hit lists its files, so CAD/STL availability needs no per-hit follow-up request).
_MODEL_FIELDS = f"""id name slug likesCount downloadCount makesCount premium price datePublished summary
  user {{ handle publicUsername }} license {{ name abbreviation }} image {{ filePath }}
  images {{ filePath }} category {{ name }} tags {{ name }} {_FILE_LISTS}"""
PAGE_SIZE = 50  # Printables answers up to 100 per call; 50 keeps each answer small
SEARCH_QUERY = (
    "query SearchModels($query: String!, $limit: Int, $offset: Int) { result: searchPrints2("
    "query: $query, printType: print, limit: $limit, offset: $offset, ordering: best_match) "
    f"{{ items {{ {_MODEL_FIELDS} }} }} }}"
)
MODEL_QUERY = f"query ModelDetail($id: ID!) {{ print(id: $id) {{ {_MODEL_FIELDS} }} }}"
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


def _media(path: str) -> str:
    return f"https://media.printables.com/{path}" if path else ""


def _files(item: dict[str, Any]) -> tuple[ModelFile, ...]:
    return tuple(
        ModelFile(name=as_str(f.get("name")), size=as_int(f.get("fileSize")),
                  ref=str(f.get("id") or ""), kind=kind)
        for key, kind in FILE_LISTS.items()
        for f in dict_list(item.get(key))
    )


def _model(item: dict[str, Any]) -> Model:
    model_id = str(item.get("id") or "")
    slug = as_str(item.get("slug"))
    user = child(item, "user")
    lic = child(item, "license")
    image = _media(as_str(child(item, "image").get("filePath")))
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
        image=image,
        files=_files(item),
        date=as_str(item.get("datePublished")),
        images=unique_urls(image, *(_media(as_str(i.get("filePath"))) for i in dict_list(item.get("images")))),
        makes=as_int(item.get("makesCount")),
        category=as_str(child(item, "category").get("name")),
        files_known=any(key in item for key in FILE_LISTS),
    )


def parse_search(data: dict[str, Any]) -> list[Model]:
    result = child(data, "result")
    return [_model(item) for item in dict_list(result.get("items"))]


def parse_model(data: dict[str, Any]) -> Model:
    item = data.get("print")
    if not isinstance(item, dict):
        raise MarketplaceError(SITE, "not-found", "model not found")
    return _model(item)


def search(query: str, limit: int) -> list[Model]:
    def page(offset: int, size: int) -> list[Model]:
        return parse_search(_graphql(SEARCH_QUERY, {"query": query, "limit": size, "offset": offset}))

    return collect_pages(page, limit, PAGE_SIZE)


def show(ref: str) -> Model:
    return parse_model(_graphql(MODEL_QUERY, {"id": ref}))


def _error_messages(result: dict[str, Any]) -> str:
    """The `errors[].messages` strings of a getDownloadLink answer, joined; entries of any
    other shape are skipped instead of hiding the real failure behind a KeyError."""
    parts: list[str] = []
    for error in dict_list(result.get("errors")):
        messages = error.get("messages")
        if isinstance(messages, list):
            text = " ".join(filter(None, map(as_str, messages)))
            if text:
                parts.append(text)
    return "; ".join(parts)


def parse_download_link(data: dict[str, Any], file: ModelFile) -> str:
    result = child(data, "getDownloadLink")
    output = child(result, "output")
    link = as_str(output.get("link"))
    if not (result.get("ok") and link):
        messages = _error_messages(result)
        raise MarketplaceError(SITE, "http", f"no download link for {file.name}"
                               + (f": {messages}" if messages else ""))
    return link


def download_link(model: Model, file: ModelFile) -> tuple[str, dict[str, str]]:
    """The signed CDN URL of one file (one getDownloadLink call), with no extra headers."""
    data = _graphql(DOWNLOAD_MUTATION, {"id": file.ref, "printId": model.id,
                                        "fileType": file.kind, "source": "model_detail"})
    return parse_download_link(data, file), {}


def download_links(model: Model) -> DownloadLinks:
    return [(file, *download_link(model, file)) for file in model.files]
