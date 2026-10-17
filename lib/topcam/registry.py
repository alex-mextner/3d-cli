"""topcam.registry — camera-node records in 3d-api, as the CLI sees them.

WHAT: `lookup_url(name)` → the URL a node registered (`PUT /v1/camera-nodes/{name}`, sent by
  `topcam.node` every minute), or None when 3d-api is not configured or has no such node;
  `ls` / `show` / `rm` implement `3d camera ls|show|rm` over the same records.
WHY: the Pi gets its address from DHCP; the record is how `--camera topcam` finds it.
INVARIANTS: stdlib at import; the API client is the shared `printops.client` (same config:
  --api/--token, PRINTOPS_API/PRINTOPS_TOKEN, ~/.config/3d-cli/printops.yaml).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from typing import Any

from topcam.client import CameraError

PREFIX = "camera"
VALUES = frozenset({"--api", "--token"})
FLAGS = frozenset({"--json"})


def lookup_url(
    name: str, *, api: str | None = None, token: str | None = None
) -> str | None:
    from printops.client import ApiClient, ApiConfigError, ApiError

    try:
        client = ApiClient.from_config(api=api, token=token)
    except ApiConfigError:
        return None
    try:
        record = client.get(_path(name))
    except ApiError as exc:
        status = getattr(exc, "status", 0)
        if status == 404:
            return None
        if status == 0:
            print(f"camera {name}: 3d-api unreachable ({exc})", file=sys.stderr)
            return None
        raise CameraError(f"camera {name}: 3d-api: {exc}") from exc
    url = record.get("url") if isinstance(record, dict) else None
    return str(url) if url else None


def _path(name: str) -> str:
    return f"/v1/camera-nodes/{urllib.parse.quote(name, safe='')}"


def _age(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _state(node: dict[str, Any]) -> str:
    return f"{'online' if node.get('online') else 'offline'} {_age(float(node.get('age_s') or 0))}"


def ls(argv: list[str]) -> int:
    """`3d camera ls`: every registered node, one line each (or the API JSON with --json)."""
    from printops.cli._common import api_errors, client, parse

    p = parse(argv, "ls", values=VALUES, flags=FLAGS, prefix=PREFIX)
    with api_errors("ls", prefix=PREFIX):
        data = client(p, "ls", prefix=PREFIX).get("/v1/camera-nodes")
    if "--json" in p.flags:
        print(json.dumps(data, indent=2))
        return 0
    items = data.get("items") or []
    if not items:
        print("no camera nodes registered (a node registers itself once api.url/api.token_file are set)")
        return 0
    rows = [(n["name"], _state(n), n["url"], n.get("card") or "-", n.get("version") or "-") for n in items]
    head = ("NAME", "STATE", "URL", "CARD", "VERSION")
    widths = [max(len(r[i]) for r in (head, *rows)) for i in range(len(head) - 1)]
    for r in (head, *rows):
        print("  ".join([*(c.ljust(w) for c, w in zip(r[:-1], widths, strict=True)), r[-1]]))
    return 0


def show(argv: list[str]) -> int:
    """`3d camera show NAME`: one node's record."""
    from printops.cli._common import api_errors, client, parse

    p = parse(argv, "show", values=VALUES, flags=FLAGS, n_pos=1, prefix=PREFIX)
    with api_errors("show", prefix=PREFIX):
        n = client(p, "show", prefix=PREFIX).get(_path(p.pos[0]))
    if "--json" in p.flags:
        print(json.dumps(n, indent=2))
        return 0
    formats = ", ".join(
        f"{fourcc} {' '.join(f'{w}x{h}' for w, h in sizes)}" for fourcc, sizes in (n.get("formats") or {}).items()
    )
    for key, value in (
        ("name", n["name"]),
        ("state", f"{_state(n)} (last seen {n['last_seen']} from {n.get('last_addr') or '?'})"),
        ("url", n["url"]),
        ("stream", n.get("stream_url") or "-"),
        ("hostname", n.get("hostname") or "-"),
        ("card", n.get("card") or "-"),
        ("device", n.get("device") or "-"),
        ("formats", formats or "-"),
        ("led", "yes" if n.get("led") else "no"),
        ("version", n.get("version") or "-"),
        ("first", n["first_seen"]),
        ("token", n["updated_by"]),
    ):
        print(f"{key:<9}{value}")
    return 0


def rm(argv: list[str]) -> int:
    """`3d camera rm NAME`: forget a node (it re-registers on its next PUT if still running)."""
    from printops.cli._common import api_errors, client, parse

    p = parse(argv, "rm", values=VALUES, n_pos=1, prefix=PREFIX)
    name = p.pos[0]
    with api_errors("rm", prefix=PREFIX):
        client(p, "rm", prefix=PREFIX).delete(_path(name))
    print(f"removed camera node {name}")
    return 0
