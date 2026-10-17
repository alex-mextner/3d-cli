"""tokens.py — 3d-api bearer tokens: a JSON file of sha256 digests with per-token scopes.

ACCESSED VIA: `3d printops token create|list|revoke` (writes the file) and
`printops.api.deps` (verifies `Authorization: Bearer <token>` against it).

WHY a file, not a DB table: a token must be mintable before the API or its DB exist
(bootstrap of the first client), and the operator can inspect/rotate it with nothing but a
shell. On the Odroid it is `/home/ultra/printops/data/api/tokens.json` (`/data/tokens.json`
inside the 3d-api container, `PRINTOPS_TOKENS_FILE`).

INVARIANTS:
  - Only `sha256(token)` is stored; the plaintext is shown once by `create`.
  - The file is written atomically (tmp + rename) with mode 0600.
  - `TokenStore` re-reads the file when its mtime/size changes, so a new or revoked token takes
    effect on the next request without restarting the API.
  - Scopes are independent (`read`, `write`, `printer-control`, `camera`); a route names the
    one(s) it accepts. `camera` is least-privilege for a camera node's SD card: only
    `PUT /v1/camera-nodes/{name}` accepts it, so a stolen card can change nothing else.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from printops.db import utc_now

SCOPES = ("read", "write", "printer-control", "camera")
TOKEN_PREFIX = "po_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class TokenError(ValueError):
    """Invalid token name/scopes, a duplicate name, or an unreadable tokens file."""


@dataclass(frozen=True)
class TokenRecord:
    name: str
    sha256: str
    scopes: tuple[str, ...]
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "scopes": list(self.scopes), "created_at": self.created_at}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_scopes(raw: str) -> tuple[str, ...]:
    scopes = tuple(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    if not scopes:
        raise TokenError(f"no scopes given; accepted: {', '.join(SCOPES)}")
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise TokenError(f"unknown scope(s) {', '.join(unknown)}; accepted: {', '.join(SCOPES)}")
    return scopes


def load(path: Path) -> list[TokenRecord]:
    """All records in the file ([] when it does not exist)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
        return [
            TokenRecord(
                name=str(t["name"]),
                sha256=str(t["sha256"]),
                scopes=tuple(str(s) for s in t["scopes"]),
                created_at=str(t.get("created_at", "")),
            )
            for t in data["tokens"]
        ]
    except (ValueError, KeyError, TypeError) as e:
        raise TokenError(f"tokens file {path} is not valid ({e}); expected {{\"tokens\": [...]}}") from e


def _save(path: Path, records: list[TokenRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"tokens": [r.to_json() for r in records]}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Exclusive cross-process lock for a read-modify-write of the tokens file: without it a
    concurrent create could save a stale copy that resurrects a token revoked meanwhile."""
    import fcntl  # POSIX-only; imported here so discovering `3d printops` subs works everywhere

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def create(path: Path, name: str, scopes: tuple[str, ...]) -> tuple[str, TokenRecord]:
    """Mint a token; returns (plaintext, record). The plaintext is not stored anywhere."""
    if not NAME_RE.match(name):
        raise TokenError(f"token name {name!r} must match {NAME_RE.pattern} (e.g. ha, bot, cli-alex)")
    for s in scopes:
        if s not in SCOPES:
            raise TokenError(f"unknown scope {s!r}; accepted: {', '.join(SCOPES)}")
    with _locked(path):
        records = load(path)
        if any(r.name == name for r in records):
            raise TokenError(f"a token named {name!r} already exists; revoke it first to rotate")
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = TokenRecord(name=name, sha256=digest(plaintext), scopes=scopes, created_at=utc_now())
        _save(path, [*records, record])
    return plaintext, record


def revoke(path: Path, name: str) -> bool:
    """Remove the named token; False when it did not exist."""
    with _locked(path):
        records = load(path)
        kept = [r for r in records if r.name != name]
        if len(kept) == len(records):
            return False
        _save(path, kept)
    return True


class TokenStore:
    """Verifies bearer tokens against the file, reloading it when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._by_digest: dict[str, TokenRecord] = {}

    def _refresh(self) -> None:
        try:
            st = self.path.stat()
            stamp: tuple[int, int, int] | None = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        records = load(self.path) if stamp is not None else []
        self._by_digest = {r.sha256: r for r in records}
        self._stamp = stamp

    def verify(self, token: str) -> TokenRecord | None:
        """The record for `token`, or None. Lookup is by sha256, so response timing reveals
        nothing about the stored digests' relation to the plaintext."""
        want = digest(token)
        with self._lock:
            self._refresh()
            return self._by_digest.get(want)

    def count(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._by_digest)
