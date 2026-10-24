"""Loopback fakes of the four marketplaces answering 50+ hits per query, for the search-core tests (#576).

`BulkServer` extends tests/marketplace_fixtures.MarketplaceServer: every site holds `total` generated
models (ids 1..total) with file lists that mix native CAD (STEP, F3D, SCAD), STL/3MF-only and archive
entries, serves them page by page through each site's own paging parameters, and records what it was asked
(`calls`, Thingiverse's follow-up counters and the highest number of simultaneous requests). Mesh files are
real binary STLs generated here (`cube_stl`), so `--deep` measures real geometry. Nothing is recorded from or
sent to a real site.
"""

from __future__ import annotations

import base64
import re
import struct
import threading
import time
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler
from typing import Any

from marketplace_fixtures import (
    CULTS_KEY,
    CULTS_USER,
    THINGIVERSE_TOKEN,
    MarketplaceServer,
)

CUBE_MM = (10.0, 20.0, 30.0)


def cube_stl(
    x: float,
    y: float,
    z: float,
    *,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    open_top: bool = False,
) -> bytes:
    """A binary STL of an axis-aligned box (12 triangles; 10 when `open_top` drops the +Z face)."""
    ox, oy, oz = origin
    v = [
        (ox + dx * x, oy + dy * y, oz + dz * z)
        for dx in (0, 1)
        for dy in (0, 1)
        for dz in (0, 1)
    ]
    centre = (ox + x / 2, oy + y / 2, oz + z / 2)
    quads = [
        (0, 1, 3, 2),
        (4, 5, 7, 6),
        (0, 1, 5, 4),
        (2, 3, 7, 6),
        (0, 2, 6, 4),
        (1, 3, 7, 5),
    ]  # +Z face last
    faces: list[tuple[int, int, int]] = []
    for a, b, c, d in quads:
        for tri in ((a, b, c), (a, c, d)):
            p0, p1, p2 = (v[i] for i in tri)
            e1 = [p1[k] - p0[k] for k in range(3)]
            e2 = [p2[k] - p0[k] for k in range(3)]
            normal = [
                e1[1] * e2[2] - e1[2] * e2[1],
                e1[2] * e2[0] - e1[0] * e2[2],
                e1[0] * e2[1] - e1[1] * e2[0],
            ]
            outward = sum(normal[k] * (p0[k] - centre[k]) for k in range(3))
            faces.append(tri if outward > 0 else (tri[0], tri[2], tri[1]))
    if open_top:
        faces = faces[:-2]
    out = bytearray(b"cube".ljust(80, b"\0") + struct.pack("<I", len(faces)))
    for i, j, k in faces:
        out += struct.pack("<12fH", 0, 0, 0, *v[i], *v[j], *v[k], 0)
    return bytes(out)


def tiny_png(
    width: int = 8, height: int = 6, rgb: tuple[int, int, int] = (220, 40, 40)
) -> bytes:
    """A solid-colour PNG built with zlib only (no Pillow needed by the fixture)."""
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _file_names(i: int) -> list[tuple[str, int]]:
    """The files of model `i`: cycles through CAD+mesh, mesh-only, multi-part, assembly and archive listings."""
    kind = i % 5
    if kind == 0:
        return [
            (f"case_{i}.step", 900_000),
            (f"case_{i}.stl", 400_000),
            (f"case_{i}.scad", 12_000),
        ]
    if kind == 1:
        return [(f"lid_{i}.stl", 150_000)]
    if kind == 2:
        return [
            (f"base_{i}.stl", 300_000),
            (f"top_{i}.stl", 250_000),
            (f"assembled_{i}.3mf", 700_000),
        ]
    if kind == 3:
        return [(f"project_{i}.f3d", 2_500_000), (f"project_{i}.stl", 800_000)]
    return [(f"all_files_{i}.zip", 5_000_000)]


class BulkServer(MarketplaceServer):
    """MarketplaceServer plus generated catalogues. `total` models per site."""

    def __init__(self, total: int = 120, *, delay_s: float = 0.0) -> None:
        super().__init__()
        self.total = total
        self.delay_s = delay_s
        self.calls: Counter[str] = Counter()
        self.page_sizes: dict[str, list[int]] = {}
        self.fail_sites: set[str] = set()
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        self.stl = cube_stl(*CUBE_MM)
        self.mesh_downloads = 0

    def __enter__(self) -> BulkServer:
        super().__enter__()
        return self

    def env(self) -> dict[str, str]:
        return {
            **super().env(),
            "THINGIVERSE_TOKEN": THINGIVERSE_TOKEN,
            "CULTS_USERNAME": CULTS_USER,
            "CULTS_API_KEY": CULTS_KEY,
        }

    # -- bookkeeping -----------------------------------------------------------------
    def _track(self, key: str, size: int | None = None) -> None:
        with self._lock:
            self.calls[key] += 1
            if size is not None:
                self.page_sizes.setdefault(key, []).append(size)

    def _busy(self) -> Any:
        server = self

        class Busy:
            def __enter__(self) -> None:
                with server._lock:
                    server.in_flight += 1
                    server.max_in_flight = max(server.max_in_flight, server.in_flight)
                if server.delay_s:
                    time.sleep(server.delay_s)

            def __exit__(self, *exc: object) -> None:
                with server._lock:
                    server.in_flight -= 1

        return Busy()

    # -- routing ---------------------------------------------------------------------
    def route_get(self, h: BaseHTTPRequestHandler) -> None:
        path, _, query = h.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if path == "/files/pic.png":
            self.send(h, 200, tiny_png(), "image/png")
        elif path == "/files/cube.stl":
            with self._lock:
                self.mesh_downloads += 1
            self.send(h, 200, self.stl, "application/octet-stream")
        elif path.startswith("/thingiverse/"):
            self.bulk_thingiverse(h, path.removeprefix("/thingiverse"), params)
        elif (
            path == "/makerworld/api/v1/search-service/select/design2"
            and "makerworld" not in self.fail_sites
        ):
            self.bulk_makerworld(h, params)
        else:
            super().route_get(h)

    def route_post(self, h: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        query = str(body.get("query") or "")
        variables = body.get("variables") or {}
        if h.path == "/printables/graphql/" and "searchPrints2" in query:
            self.bulk_printables(h, variables)
        elif h.path == "/printables/graphql/" and "ModelDetail" in query:
            self._track("printables:detail")
            i = int(variables.get("id") or 0)
            self.send(
                h,
                200,
                {
                    "data": {
                        "print": self.printables_item(i)
                        if 1 <= i <= self.total
                        else None
                    }
                },
            )
        elif h.path == "/printables/graphql/" and "getDownloadLink" in query:
            self._track("printables:download-link")
            self.send(
                h,
                200,
                {
                    "data": {
                        "getDownloadLink": {
                            "ok": True,
                            "errors": [],
                            "output": {"link": self.file_url("cube.stl")},
                        }
                    }
                },
            )
        elif h.path == "/cults/graphql" and "creationsSearchBatch" in query:
            self.bulk_cults(h, variables)
        elif (
            h.path == "/cults/graphql"
            and "creation(slug" in query
            and str(variables.get("slug", "")).startswith("creation-")
        ):
            self.bulk_cults_creation(h, str(variables["slug"]))
        else:
            super().route_post(h, body)

    def _page(self, offset: int, size: int) -> range:
        return range(offset + 1, min(offset + size, self.total) + 1)

    # -- Printables ------------------------------------------------------------------
    def printables_item(self, i: int) -> dict[str, Any]:
        files = _file_names(i)
        meshes = (".stl", ".3mf")
        return {
            "id": str(i),
            "name": f"Printables model {i}",
            "slug": f"model-{i}",
            "likesCount": i,
            "downloadCount": i * 10,
            "makesCount": i // 2,
            "premium": False,
            "price": None,
            "datePublished": "2026-01-31T09:18:54+00:00",
            "summary": f"Summary of printables model {i}",
            "user": {"handle": "maker", "publicUsername": "Maker"},
            "license": {"name": "Creative Commons", "abbreviation": "CC-BY"},
            "image": {"filePath": f"media/prints/{i}/cover.png"},
            "images": [
                {"filePath": f"media/prints/{i}/cover.png"},
                {"filePath": f"media/prints/{i}/2.png"},
            ],
            "category": {"name": "Gadgets"},
            "tags": [{"name": "case"}, {"name": f"tag{i}"}],
            "stls": [
                {"id": f"{i}{n}", "name": name, "fileSize": size_b}
                for n, (name, size_b) in enumerate(files)
                if name.endswith(meshes)
            ],
            "gcodes": [],
            "slas": [],
            "otherFiles": [
                {"id": f"{i}{n}", "name": name, "fileSize": size_b}
                for n, (name, size_b) in enumerate(files)
                if not name.endswith(meshes)
            ],
        }

    def bulk_printables(
        self, h: BaseHTTPRequestHandler, variables: dict[str, Any]
    ) -> None:
        offset, size = (
            int(variables.get("offset") or 0),
            int(variables.get("limit") or 20),
        )
        self._track("printables:search", size)
        if "printables" in self.fail_sites:
            self.send(h, 200, {"errors": [{"message": "search is down"}]})
            return
        items = [self.printables_item(i) for i in self._page(offset, size)]
        self.send(
            h, 200, {"data": {"result": {"totalCount": self.total, "items": items}}}
        )

    # -- MakerWorld ------------------------------------------------------------------
    def bulk_makerworld(
        self, h: BaseHTTPRequestHandler, params: dict[str, str]
    ) -> None:
        offset, size = int(params.get("offset", 0)), int(params.get("limit", 20))
        self._track("makerworld:search", size)
        hits = []
        for i in self._page(offset, size):
            hits.append(  # noqa: PERF401 — one literal per hit reads better than a comprehension
                {
                    "id": i,
                    "title": f"MakerWorld model {i}",
                    "slug": f"model-{i}",
                    "cover": f"https://cdn.example/mw/{i}.jpg",
                    "likeCount": i,
                    "downloadCount": i * 5,
                    "printCount": i // 3,
                    "createTime": "2025-03-02T10:00:00Z",
                    "designCreator": {"name": "MW Maker", "handle": "mwmaker"},
                    "license": "BY-NC-SA",
                    "tags": ["case", f"tag{i}"],
                    "is_point_redeemable": False,
                    "designExtension": {
                        "design_pictures": [
                            {"name": "cover", "url": f"https://cdn.example/mw/{i}.jpg"},
                            {
                                "name": "1.jpg",
                                "url": f"https://cdn.example/mw/{i}-1.jpg",
                            },
                        ],
                        "model_files": [
                            {
                                "modelName": name,
                                "modelSize": size_b,
                                "modelType": name.rpartition(".")[2],
                                "modelUrl": "",
                                "isDir": False,
                                "children": [],
                            }
                            for name, size_b in _file_names(i)
                        ],
                    },
                }
            )
        self.send(h, 200, {"total": self.total, "hits": hits})

    # -- Cults -----------------------------------------------------------------------
    def cults_item(self, i: int, *, links: bool) -> dict[str, Any]:
        return {
            "identifier": f"creation-{i}",
            "name": f"Cults model {i}",
            "url": f"https://cults3d.com/en/3d-model/home/creation-{i}",
            "likesCount": i,
            "downloadsCount": i * 2,
            "viewsCount": i * 20,
            "publishedAt": "2025-05-02T10:45:54Z",
            "illustrationImageUrl": f"https://img.example/cults/{i}.jpg",
            "illustrations": [{"imageUrl": f"https://img.example/cults/{i}.jpg"}],
            "category": {"name": "Home"},
            "creator": {"nick": "cultsmaker", "shortUrl": "https://cults3d.com/:1"},
            "license": {"name": "CC BY"},
            "price": {"cents": 0 if i % 4 else 150},
            "description": f"Description of cults model {i}",
            "tags": ["case"],
            "blueprints": [
                {
                    "fileName": name,
                    "fileUrl": self.file_url("cube.stl") if links else None,
                }
                for name, _ in _file_names(i)
            ],
        }

    def _cults_authorised(self, h: BaseHTTPRequestHandler) -> bool:
        expected = base64.b64encode(f"{CULTS_USER}:{CULTS_KEY}".encode()).decode()
        if h.headers.get("Authorization") == f"Basic {expected}":
            return True
        self.send(h, 401, b"HTTP Basic: Access denied.\n", "text/html")
        return False

    def bulk_cults(self, h: BaseHTTPRequestHandler, variables: dict[str, Any]) -> None:
        if not self._cults_authorised(h):
            return
        offset, size = (
            int(variables.get("offset") or 0),
            int(variables.get("limit") or 20),
        )
        self._track("cults:search", size)
        if "cults" in self.fail_sites:
            self.send(h, 200, {"errors": [{"message": "search is down"}]})
            return
        results = [self.cults_item(i, links=False) for i in self._page(offset, size)]
        self.send(
            h, 200, {"data": {"result": {"total": self.total, "results": results}}}
        )

    def bulk_cults_creation(self, h: BaseHTTPRequestHandler, slug: str) -> None:
        if not self._cults_authorised(h):
            return
        found = re.fullmatch(r"creation-(\d+)", slug)
        i = int(found.group(1)) if found else 0
        self._track("cults:detail")
        self.send(
            h,
            200,
            {
                "data": {
                    "creation": self.cults_item(i, links=True)
                    if 1 <= i <= self.total
                    else None
                }
            },
        )

    # -- Thingiverse -----------------------------------------------------------------
    def bulk_thingiverse(
        self, h: BaseHTTPRequestHandler, path: str, params: dict[str, str]
    ) -> None:
        if h.headers.get("Authorization") != f"Bearer {THINGIVERSE_TOKEN}":
            self.send(h, 401, {"error": "Unauthorized", "type": "NO_TOKEN_PROVIDED"})
            return
        if path.startswith("/search/"):
            size, page = int(params.get("per_page", 20)), int(params.get("page", 1))
            self._track("thingiverse:search", size)
            hits = [
                {
                    "id": i,
                    "name": f"Thingiverse model {i}",
                    "public_url": f"https://www.thingiverse.com/thing:{i}",
                    "thumbnail": self.file_url("pic.png"),
                    "preview_image": self.file_url("pic.png"),
                    "like_count": i,
                    "added": "2024-06-01T00:00:00+00:00",
                    "creator": {
                        "name": "tvmaker",
                        "public_url": "https://www.thingiverse.com/tvmaker",
                    },
                }
                for i in self._page((page - 1) * size, size)
            ]
            self.send(h, 200, {"total": self.total, "hits": hits})
            return
        found = re.fullmatch(r"/things/(\d+)(/files)?", path)
        if found is None:
            self.send(h, 404, {"error": "Not Found"})
            return
        i = int(found.group(1))
        if not 1 <= i <= self.total:
            self.send(h, 404, {"error": "Not Found"})
            return
        self._track("thingiverse:files" if found.group(2) else "thingiverse:detail")
        with (
            self._busy()
        ):  # held while the request is "served"; released BEFORE the answer is written
            pass
        if found.group(2):
            self.send(
                h,
                200,
                [
                    {
                        "name": name,
                        "size": size_b,
                        "direct_url": self.file_url("cube.stl"),
                    }
                    for name, size_b in _file_names(i)
                ],
            )
        else:
            self.send(
                h,
                200,
                {
                    "id": i,
                    "name": f"Thingiverse model {i}",
                    "license": "Creative Commons - Attribution",
                    "download_count": i * 7,
                    "view_count": i * 30,
                    "description": f"Thing {i} description",
                    "tags": [{"name": "case"}],
                },
            )
