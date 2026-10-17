"""MakerWorld (Bambu Lab) through the JSON API its website calls.

makerworld.com sits behind Cloudflare bot management that rejects urllib's TLS fingerprint
(403 "Just a moment..."), but lets a Chrome TLS/HTTP2 fingerprint through, so every call
goes over transport.fetch_impersonated. Search and design metadata need no login; files do —
the download endpoint answers 403 "Please log in to download models." without the `token`
cookie of a logged-in account (credentials.makerworld_token). Paid and point-redeemable
designs are refused. ("Exclusive" designs are free to download — the badge only means the
creator publishes them on MakerWorld alone — so they are not treated as paid.)
"""
from __future__ import annotations

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
    dict_list,
    require_dict,
    str_list,
)

SITE = "makerworld"
WEB_URL = "https://makerworld.com"
_URL_RE = re.compile(r"^https?://(?:www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)",
                     re.I)


def base_url() -> str:
    return os.environ.get("THREED_MAKERWORLD_URL", WEB_URL).rstrip("/")


def match(url: str) -> str | None:
    found = _URL_RE.match(url.strip())
    return found.group(1) if found else None


def _get(path: str, *, headers: dict[str, str] | None = None, auth_hint: str = "") -> Any:
    response = transport.fetch_impersonated(SITE, f"{base_url()}{path}", headers=headers)
    transport.raise_for_status(SITE, response, auth_hint=auth_hint)
    return transport.decode_json(SITE, response)


def _model(design: dict[str, Any], *, files: tuple[ModelFile, ...] = ()) -> Model:
    creator = child(design, "designCreator")
    design_id = str(design.get("id") or "")
    slug = as_str(design.get("slug"))
    paid_setting = child(design, "paidSetting")
    redeemable = bool(design.get("isPointRedeemable") or design.get("is_point_redeemable"))
    paid = bool(paid_setting.get("isPaid")) or redeemable
    handle = as_str(creator.get("handle"))
    return Model(
        site=SITE,
        id=design_id,
        title=as_str(design.get("title")),
        url=f"{WEB_URL}/en/models/{design_id}-{slug}" if slug else f"{WEB_URL}/en/models/{design_id}",
        author=as_str(creator.get("name")) or handle,
        author_url=f"{WEB_URL}/en/@{handle}" if handle else "",
        likes=as_int(design.get("likeCount")),
        downloads=as_int(design.get("downloadCount")),
        license=as_str(design.get("license")),
        price="points" if redeemable else ("paid" if paid else ""),
        paid=paid,
        summary=re.sub(r"<[^>]+>", " ", as_str(design.get("summary"))).strip()[:500],
        tags=str_list(design.get("tags")),
        image=as_str(design.get("cover")) or as_str(design.get("coverUrl")),
        files=files,
    )


def parse_search(payload: Any) -> list[Model]:
    """GET /api/v1/search-service/select/design2 → {"total": N, "hits": [design, ...]}."""
    hits = payload.get("hits") if isinstance(payload, dict) else None
    return [_model(design) for design in dict_list(hits)]


def parse_model(payload: Any) -> Model:
    """GET /api/v1/design-service/design/{id}: raw model files are listed under
    designExtension.model_files (names/sizes only; URLs need a login)."""
    design = require_dict(SITE, payload, "design")
    extension = child(design, "designExtension")
    files = tuple(
        ModelFile(name=as_str(f.get("modelName")), size=as_int(f.get("modelSize")),
                  ref=as_str(f.get("modelUrl")), kind=as_str(f.get("modelType")))
        for f in dict_list(extension.get("model_files"))
    )
    return _model(design, files=files)


def search(query: str, limit: int) -> list[Model]:
    params = urllib.parse.urlencode({"keyword": query, "limit": str(limit), "offset": "0"})
    return parse_search(_get(f"/api/v1/search-service/select/design2?{params}"))[:limit]


def show(ref: str) -> Model:
    return parse_model(_get(f"/api/v1/design-service/design/{ref}"))


def parse_download(payload: Any) -> tuple[str, str]:
    """The download endpoint answers {"name": "<archive>.zip", "url": "<signed CDN URL>"}."""
    data = require_dict(SITE, payload, "download")
    url = as_str(data.get("url"))
    if not url:
        raise MarketplaceError(SITE, "http", f"no download URL in the answer: {sorted(data)}")
    return as_str(data.get("name")), url


def download_links(model: Model) -> DownloadLinks:
    """One archive with every raw model file of the design (the website's "Download STL/CAD
    files" button)."""
    token = credentials.makerworld_token()
    hint = ("MakerWorld refused the token: log in again in a browser and copy a fresh `token` "
            "cookie into auth.json `makerworld.token` / MAKERWORLD_TOKEN.")
    payload = _get(f"/api/v1/design-service/design/{model.id}/model?type=download",
                   headers={"Authorization": f"Bearer {token}", "Cookie": f"token={token}"},
                   auth_hint=hint)
    name, url = parse_download(payload)
    archive = ModelFile(name=name or f"makerworld-{model.id}.zip", kind="archive", ref=url)
    return [(archive, url, {})]
