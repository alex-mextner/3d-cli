"""camera_nodes.py — the camera-node registry: where each top-camera node (topcam) answers.

WHAT: one `camera_nodes` row per node (migration 0090, issue #341). A node upserts its own row
  (`PUT /v1/camera-nodes/{name}`, sent by `topcam.node` every `api.interval_s`, default 60 s);
  `3d camera ls|show|rm` and `topcam.registry.lookup_url` read it.

ACCESSED VIA: lib/printops/api/routes_cameras.py only.

INVARIANTS:
  - `name` follows the fleet printer-name rule ([A-Za-z0-9][A-Za-z0-9_.-]*, here ≤ 64 chars).
  - `url` and `stream_url` are absolute http(s) URLs with a host; anything else is refused, so
    a client that follows a record never opens a file:// or other-scheme URL.
  - Text fields carry no control characters (they are printed by `3d camera ls/show`) and
    `formats` is bounded (MAX_FOURCCS × MAX_SIZES), so a node token cannot bloat the DB.
  - A `camera`-only token writes only the node named like the token (routes_cameras).
  - The body is the node's `Node.record()`; unknown keys are refused (a typo must not register
    a half-empty record silently).
  - `online` is derived at read time: last_seen within ONLINE_S (3 default node intervals).
  - The first registration of a name emits `camera.registered`; refreshes emit nothing (one
    PUT a minute per node would flood the event log).
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from printops import db, events

ONLINE_S = 180  # 3 × the node's default registration interval (api.interval_s = 60)
MAX_TEXT = 512
MAX_FOURCCS = 16  # a UVC camera lists a handful of pixel formats
MAX_SIZES = 64  # … and a few dozen frame sizes per format
MAX_DIM = 65535  # V4L2 frame width/height
_CONTROL = re.compile(
    r"[\x00-\x1f\x7f-\x9f]"
)  # printed by `3d camera ls/show`: no terminal escapes
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
BODY_KEYS = frozenset(
    {"url", "stream_url", "hostname", "card", "device", "formats", "led", "version"}
)
_TEXT_KEYS = ("hostname", "card", "device", "version")


class CameraNodeError(ValueError):
    """Invalid node name or registration body (the API answers 422)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CameraNode:
    name: str
    url: str
    stream_url: str | None
    hostname: str | None
    card: str | None
    device: str | None
    formats: dict[str, list[list[int]]]
    led: bool
    version: str | None
    first_seen: str
    last_seen: str
    last_addr: str | None
    updated_by: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CameraNode:
        return cls(
            name=str(row["name"]),
            url=str(row["url"]),
            stream_url=row["stream_url"],
            hostname=row["hostname"],
            card=row["card"],
            device=row["device"],
            formats=json.loads(row["formats"]),
            led=bool(row["led"]),
            version=row["version"],
            first_seen=str(row["first_seen"]),
            last_seen=str(row["last_seen"]),
            last_addr=row["last_addr"],
            updated_by=str(row["updated_by"]),
        )

    def to_dict(self, now: str) -> dict[str, Any]:
        age = max(0.0, (_parse(now) - _parse(self.last_seen)).total_seconds())
        return {
            "name": self.name,
            "url": self.url,
            "stream_url": self.stream_url,
            "hostname": self.hostname,
            "card": self.card,
            "device": self.device,
            "formats": self.formats,
            "led": self.led,
            "version": self.version,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_addr": self.last_addr,
            "updated_by": self.updated_by,
            "age_s": round(age, 1),
            "online": age <= ONLINE_S,
        }


def _parse(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise CameraNodeError(
            f"camera node name {name!r} must be letters/digits/_ . - (starting with a letter or digit, ≤ 64 chars)"
        )
    return name


def _url(value: object, key: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT or _CONTROL.search(value):
        raise CameraNodeError(f"{key} must be an http(s) URL string")
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError as exc:
        raise CameraNodeError(f"{key} is not a valid URL ({exc}): {value!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CameraNodeError(
            f"{key} must be an absolute http:// or https:// URL, got {value!r}"
        )
    return value


def _text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT or _CONTROL.search(value):
        raise CameraNodeError(
            f"{key} must be a string of at most {MAX_TEXT} printable chars or null"
        )
    return value


def _formats(value: object) -> dict[str, list[list[int]]]:
    if value is None:
        return {}
    bad = CameraNodeError(
        'formats must be an object {"FOURCC": [[width, height], …]} with at most '
        f"{MAX_FOURCCS} formats of at most {MAX_SIZES} sizes, each 1..{MAX_DIM}"
    )
    if not isinstance(value, dict) or len(value) > MAX_FOURCCS:
        raise bad
    out: dict[str, list[list[int]]] = {}
    for fourcc, sizes in value.items():
        if (
            not isinstance(fourcc, str)
            or not fourcc
            or len(fourcc) > 16
            or _CONTROL.search(fourcc)
            or not isinstance(sizes, list)
            or len(sizes) > MAX_SIZES
        ):
            raise bad
        clean: list[list[int]] = []
        for size in sizes:
            if not (
                isinstance(size, list)
                and len(size) == 2
                and all(
                    isinstance(n, int) and not isinstance(n, bool) and 0 < n <= MAX_DIM
                    for n in size
                )
            ):
                raise bad
            clean.append([size[0], size[1]])
        out[fourcc] = clean
    return out


def validate(body: Mapping[str, Any]) -> dict[str, Any]:
    """The registration body as column values, or CameraNodeError."""
    unknown = sorted(set(body) - BODY_KEYS)
    if unknown:
        raise CameraNodeError(
            f"unknown field(s) {', '.join(unknown)}; accepted: {', '.join(sorted(BODY_KEYS))}"
        )
    if "url" not in body:
        raise CameraNodeError("url is required")
    led = body.get("led", False)
    if not isinstance(led, bool):
        raise CameraNodeError("led must be true or false")
    stream = body.get("stream_url")
    return {
        "url": _url(body["url"], "url"),
        "stream_url": None if stream is None else _url(stream, "stream_url"),
        **{k: _text(body.get(k), k) for k in _TEXT_KEYS},
        "formats": _formats(body.get("formats")),
        "led": led,
    }


def get(conn: sqlite3.Connection, name: str) -> CameraNode | None:
    row = conn.execute("SELECT * FROM camera_nodes WHERE name = ?", (name,)).fetchone()
    return None if row is None else CameraNode.from_row(row)


def list_nodes(conn: sqlite3.Connection) -> list[CameraNode]:
    return [
        CameraNode.from_row(r)
        for r in conn.execute("SELECT * FROM camera_nodes ORDER BY name")
    ]


def upsert(
    conn: sqlite3.Connection,
    name: str,
    body: Mapping[str, Any],
    *,
    addr: str | None,
    actor: str,
) -> tuple[CameraNode, bool]:
    """Insert or refresh the node's row; returns (record, created)."""
    check_name(name)
    v = validate(body)
    now = db.utc_now()
    cols = {
        **v,
        "formats": json.dumps(v["formats"], separators=(",", ":")),
        "led": int(v["led"]),
        "last_seen": now,
        "last_addr": addr,
        "updated_by": actor,
    }
    with db.transaction(conn):
        prev = conn.execute(
            "SELECT first_seen FROM camera_nodes WHERE name = ?", (name,)
        ).fetchone()
        if prev is None:
            row = {"name": name, "first_seen": now, **cols}
            conn.execute(
                f"INSERT INTO camera_nodes ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            events.emit(
                conn,
                "camera.registered",
                f"camera:{name}",
                {"url": v["url"], "card": v["card"], "version": v["version"]},
                actor,
            )
        else:
            conn.execute(
                f"UPDATE camera_nodes SET {', '.join(f'{k} = ?' for k in cols)} WHERE name = ?",
                (*cols.values(), name),
            )
    first_seen = now if prev is None else str(prev["first_seen"])
    node = CameraNode(
        name=name,
        first_seen=first_seen,
        last_seen=now,
        last_addr=addr,
        updated_by=actor,
        **v,
    )
    return node, prev is None


def delete(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("DELETE FROM camera_nodes WHERE name = ?", (name,)).rowcount > 0
