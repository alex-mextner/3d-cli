"""Loopback fakes of the four marketplace APIs for `3d models` tests.

`MarketplaceServer` serves the recorded/documented responses in tests/data/marketplaces/
over http://127.0.0.1:<port>, with file links rewritten to its own /files/ route, and
`env()` points the clients at it (THREED_*_API overrides). `write_fake_curl()` writes a
stand-in for a curl-impersonate binary (`curl_chrome146`) that speaks the subset of curl's
CLI the MakerWorld lane uses (-o, -H @file, -w '%{http_code} %{url_effective}').
No test touches a real site.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data" / "marketplaces"
THINGIVERSE_TOKEN = "tv-test-token"
CULTS_USER, CULTS_KEY = "tester", "cults-test-key"
MAKERWORLD_TOKEN = "mw-test-token"
FILES = {
    "spoon_handle.stl": b"solid spoon_handle\nendsolid spoon_handle\n",
    "spoon_pull.stl": b"solid tv1\nendsolid tv1\n",
    "spoon_pull_v2.stl": b"solid tv2\nendsolid tv2\n",
    "spoon-pull.stl": b"solid cults\nendsolid cults\n",
    "makerworld-1871683.zip": b"PK\x05\x06" + b"\x00" * 18,
}


def load(name: str) -> Any:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


class MarketplaceServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        # /files/<name> in here answers with a Content-Length larger than the body it sends
        # (a connection dropped mid-download).
        self.truncated: set[str] = set()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # keep test output quiet
                return

            def do_GET(self) -> None:  # noqa: N802 — http.server API
                server.requests.append(("GET", self.path, dict(self.headers)))
                server.route_get(self)

            def do_POST(self) -> None:  # noqa: N802 — http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append(("POST", self.path, dict(self.headers)))
                server.route_post(self, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> MarketplaceServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def env(self) -> dict[str, str]:
        return {
            "THREED_PRINTABLES_API": f"{self.base}/printables/graphql/",
            "THREED_THINGIVERSE_API": f"{self.base}/thingiverse",
            "THREED_CULTS_API": f"{self.base}/cults/graphql",
            "THREED_MAKERWORLD_URL": f"{self.base}/makerworld",
        }

    def file_url(self, name: str) -> str:
        return f"{self.base}/files/{name}"

    # -- responses ---------------------------------------------------------------------
    @staticmethod
    def send(handler: BaseHTTPRequestHandler, status: int, payload: Any,
             content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def route_get(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?", 1)[0]
        if path.startswith("/files/"):
            name = path.removeprefix("/files/")
            if name in self.truncated:
                h.send_response(200)
                h.send_header("Content-Length", str(len(FILES[name]) + 1000))
                h.end_headers()
                h.wfile.write(FILES[name])  # HTTP/1.0: the connection closes short
            elif name in FILES:
                self.send(h, 200, FILES[name], "application/octet-stream")
            else:
                self.send(h, 404, {"error": "no such file"})
        elif path.startswith("/redirect/"):
            # A CDN hand-off to another host name (localhost instead of 127.0.0.1).
            h.send_response(302)
            h.send_header("Location", f"http://localhost:{self.httpd.server_address[1]}"
                                      f"/files/{path.removeprefix('/redirect/')}")
            h.send_header("Content-Length", "0")
            h.end_headers()
        elif path.startswith("/thingiverse/"):
            self.thingiverse(h, path.removeprefix("/thingiverse"))
        elif path.startswith("/makerworld/"):
            self.makerworld(h, path.removeprefix("/makerworld"))
        else:
            self.send(h, 404, {"error": "unknown route"})

    def route_post(self, h: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        query = str(body.get("query") or "")
        variables = body.get("variables") or {}
        if h.path == "/printables/graphql/":
            if "searchPrints2" in query:
                self.send(h, 200, load("printables_search"))
            elif "getDownloadLink" in query:
                link = load("printables_download_link")
                link["data"]["getDownloadLink"]["output"]["link"] = self.file_url("spoon_handle.stl")
                self.send(h, 200, link)
            elif variables.get("id") == "311253":
                self.send(h, 200, load("printables_model"))
            else:
                self.send(h, 200, {"data": {"print": None}})
        elif h.path == "/cults/graphql":
            expected = base64.b64encode(f"{CULTS_USER}:{CULTS_KEY}".encode()).decode()
            if h.headers.get("Authorization") != f"Basic {expected}":
                self.send(h, 401, b"HTTP Basic: Access denied.\n", "text/html")
            elif "creationsSearchBatch" in query:
                self.send(h, 200, load("cults_search"))
            elif variables.get("slug") == "luxury-spoon-pull":
                self.send(h, 200, load("cults_creation_paid"))
            else:
                creation = load("cults_creation")
                creation["data"]["creation"]["blueprints"][0]["fileUrl"] = (
                    self.file_url("spoon-pull.stl") + "?sig=x")
                self.send(h, 200, creation)
        else:
            self.send(h, 404, {"error": "unknown route"})

    def thingiverse(self, h: BaseHTTPRequestHandler, path: str) -> None:
        if h.headers.get("Authorization") != f"Bearer {THINGIVERSE_TOKEN}":
            self.send(h, 401, {"error": "Unauthorized access. No authentication was provided.",
                               "code": 401, "type": "NO_TOKEN_PROVIDED"})
        elif path.startswith("/search/"):
            self.send(h, 200, load("thingiverse_search"))
        elif path == "/things/5594736":
            self.send(h, 200, load("thingiverse_thing"))
        elif path == "/things/4000001":
            self.send(h, 200, load("thingiverse_thing_4000001"))
        elif path == "/things/5594736/files":
            files = load("thingiverse_files")
            files[0]["direct_url"] = self.file_url("spoon_pull.stl")
            files[1]["download_url"] = self.file_url("spoon_pull_v2.stl")
            self.send(h, 200, files)
        else:
            self.send(h, 404, {"error": "Not Found"})

    def makerworld(self, h: BaseHTTPRequestHandler, path: str) -> None:
        if path == "/api/v1/search-service/select/design2":
            self.send(h, 200, load("makerworld_search"))
        elif path == "/api/v1/design-service/design/1871683":
            self.send(h, 200, load("makerworld_design"))
        elif path == "/api/v1/design-service/design/1871683/model":
            if h.headers.get("Authorization") != f"Bearer {MAKERWORLD_TOKEN}":
                self.send(h, 403, {"code": 1, "error": "Please log in to download models."})
            else:
                self.send(h, 200, {"name": "makerworld-1871683.zip",
                                   "url": self.file_url("makerworld-1871683.zip")})
        else:
            self.send(h, 404, {"error": "unknown route"})


FAKE_CURL = '''#!{python}
"""Stand-in for curl_chrome146: -sS -L --max-time N -o OUT -H @FILE -w FMT URL."""
import sys, urllib.error, urllib.request
args = sys.argv[1:]
out = headers = None
hdrs = {{}}
i = 0
url = ""
while i < len(args):
    a = args[i]
    if a in ("-o", "-H", "-w", "--max-time"):
        v = args[i + 1]
        if a == "-o":
            out = v
        elif a == "-H" and v.startswith("@"):
            for line in open(v[1:], encoding="utf-8"):
                k, sep, val = line.partition(":")
                if sep:
                    hdrs[k.strip()] = val.strip()
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
open({log!r}, "a").write(url + "\\n")
req = urllib.request.Request(url, headers=hdrs)
try:
    with urllib.request.urlopen(req) as r:
        status, body = r.status, r.read()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read()
open(out, "wb").write(body)
sys.stdout.write(f"{{status}} {{url}}")
'''


def write_fake_curl(directory: Path) -> tuple[Path, Path]:
    """A fake `curl_chrome146` in `directory`; returns (binary, log of fetched URLs)."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "curl.log"
    binary = directory / "curl_chrome146"
    binary.write_text(FAKE_CURL.format(python=sys.executable, log=str(log)), encoding="utf-8")
    binary.chmod(0o755)
    return binary, log
