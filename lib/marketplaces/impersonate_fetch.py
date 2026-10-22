"""impersonate_fetch.py — GET one URL with a Chrome TLS/HTTP2 fingerprint via curl_cffi.

Run by marketplaces.http through cli.pyrun (repo .venv, else `uv run --with curl_cffi`), so
the 3d process itself never needs curl_cffi installed:

    python impersonate_fetch.py URL OUT_FILE HEADERS_FILE follow|no-follow

HEADERS_FILE holds one "Name: value" per line (a file, not argv, because it may carry a
token). Writes the response body to OUT_FILE and prints "<status> <final url>" — the same
line `curl -w '%{http_code} %{url_effective}'` prints, so the caller parses both lanes alike.
Exit 0 whenever an HTTP response arrived (any status); exit 1 with the reason on stderr when
the request itself failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from curl_cffi import requests


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[3] not in ("follow", "no-follow"):
        print("usage: impersonate_fetch.py URL OUT_FILE HEADERS_FILE follow|no-follow", file=sys.stderr)
        return 2
    url, out, header_file = argv[0], Path(argv[1]), Path(argv[2])
    follow = argv[3] == "follow"  # no-follow when the headers carry a credential
    headers = {}
    for line in header_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip()] = value.strip()
    try:
        reply = requests.get(url, headers=headers, impersonate="chrome", timeout=30,
                             allow_redirects=follow)
    except Exception as exc:  # noqa: BLE001 — curl_cffi has its own error tree; report any
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    out.write_bytes(reply.content)
    print(f"{reply.status_code} {reply.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
