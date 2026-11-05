"""ha_detect.py — is there a Home Assistant around? READ-ONLY detection + where its token comes from.

ACCESSED VIA: `printops.env_stage` (`3d init --env`), `3d printops ha detect`.

DETECTION (no write, no login; each probe is one GET of a public page):
  1. configured: `$HA_URL` / `$PRINTOPS_HA_URL`, `~/.config/3d-cli/ha.yaml` (`url:`);
  2. mDNS: one `_home-assistant._tcp.local.` PTR query (UDP multicast, ~1.5 s) — HA announces itself;
  3. known names/ports: `homeassistant.local`, `homeassistant`, `localhost` and the Spoolman host
     (HA often runs on the same box) on :8123.
  A URL counts as Home Assistant when its `/manifest.json` names "Home Assistant" or `/api/` answers 401
  with HA's text. Nothing is probed that the caller did not list; the Spoolman host is passed in only
  after the user chose it.

TOKEN (`resolve_token`): `$HA_TOKEN`, then `~/.config/3d-cli/ha.yaml` `token:` — refused when the file is
group/other readable (fix: chmod 600) —, then a prompt. A prompted token is stored (mode 600) only when
the caller says so (after the user confirmed). This is the same long-lived-token concept as the HA
deploy's `HA_TOKEN` in the HA repo's `.env` (`3d ha deploy`), and `PRINTOPS_HA_TOKEN_FILE` of 3d-api.
"""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import struct
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import time
from cli import paths
from errors import UsageError

HA_PORT = 8123
MDNS_NAME = "_home-assistant._tcp.local"
Fetch = Callable[
    [str], tuple[int, str]
]  # url -> (http status, body); status 0 = unreachable


@dataclass(frozen=True)
class HaFound:
    url: str
    via: str  # configured | mdns | known name | spoolman host


def config_file() -> Path:
    return paths.config_dir() / "ha.yaml"


def load_ha_config(path: Path | None = None) -> dict[str, Any]:
    """`~/.config/3d-cli/ha.yaml`; a token in a group/other-readable file is refused, not used."""
    path = path or config_file()
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise UsageError(
            f"{path}: expected a mapping (url:, token:, ssh:, config_dir:)",
            command="ha",
        )
    if data.get("token") and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UsageError(
            f"{path} holds a token but is readable by others",
            command="ha",
            remediation=[f"chmod 600 {path}"],
        )
    return data


def save_ha_config(updates: Mapping[str, Any], path: Path | None = None) -> Path:
    """Merge `updates` into ha.yaml (atomic, mode 600)."""
    import yaml

    path = path or config_file()
    data = load_ha_config(path)
    data.update({k: v for k, v in updates.items() if v})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ha.yaml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def resolve_token(
    env: Mapping[str, str],
    ask: Callable[[str], str] | None = None,
    path: Path | None = None,
) -> tuple[str | None, str]:
    """(token, where it came from) — `(None, "")` when there is none and no `ask`."""
    if env.get("HA_TOKEN"):
        return env["HA_TOKEN"].strip(), "$HA_TOKEN"
    cfg = load_ha_config(path)
    if cfg.get("token"):
        return str(cfg["token"]).strip(), str(path or config_file())
    if ask is not None:
        typed = ask(
            "Home Assistant long-lived access token (Profile -> Security -> Long-lived access tokens; not echoed): "
        )
        if typed.strip():
            return typed.strip(), "prompt"
    return None, ""


# ------------------------------------------------------------------------------- probing


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "3d-cli/ha-detect"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — a URL the user/LAN named
            return int(resp.status), resp.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(2000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def is_home_assistant(url: str, fetch: Fetch = http_get) -> bool:
    status, body = fetch(f"{url.rstrip('/')}/manifest.json")
    if status == 200 and '"Home Assistant"' in body.replace("'", '"'):
        return True
    status, body = fetch(f"{url.rstrip('/')}/api/")
    return status == 401 and "Unauthorized" in body


def _query() -> bytes:
    name = (
        b"".join(bytes([len(p)]) + p.encode() for p in MDNS_NAME.split(".")) + b"\x00"
    )
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + name + struct.pack(">HH", 12, 1)


def _pointer(buf: bytes, pos: int) -> int:
    if pos + 1 >= len(buf):
        raise ValueError("truncated DNS compression pointer")
    return ((buf[pos] & 0x3F) << 8) | buf[pos + 1]


def _label(buf: bytes, pos: int, n: int) -> str:
    if pos + 1 + n > len(buf):
        raise ValueError("truncated DNS label")
    return buf[pos + 1 : pos + 1 + n].decode("utf-8", "replace")


def _read_name(buf: bytes, pos: int) -> tuple[str, int]:
    """A DNS name at `pos`. Each compression-pointer target is followed at most once: a packet whose
    pointers loop (`c0 0c` at offset 12 points at itself) or that is cut short raises ValueError instead
    of spinning — the packet comes from anyone on the LAN."""
    labels: list[str] = []
    jumped_to = -1
    visited: set[int] = set()
    while pos < len(buf):
        n = buf[pos]
        if n == 0:
            return ".".join(labels), jumped_to if jumped_to >= 0 else pos + 1
        if n & 0xC0 == 0xC0:
            jumped_to = pos + 2 if jumped_to < 0 else jumped_to
            pos = _pointer(buf, pos)
            if pos in visited:
                raise ValueError("DNS compression pointer loop")
            visited.add(pos)
            continue
        labels.append(_label(buf, pos, n))
        pos += 1 + n
    raise ValueError("truncated DNS name")


def parse_mdns(buf: bytes) -> list[str]:
    """`http://IP:PORT` for every Home Assistant service announced in one mDNS packet."""
    try:
        _id, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", buf[:12])
        pos = 12
        for _ in range(qd):
            _, pos = _read_name(buf, pos)
            pos += 4
        ports: dict[str, int] = {}
        addrs: dict[str, str] = {}
        for _ in range(an + ns + ar):
            name, pos = _read_name(buf, pos)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[pos : pos + 10])
            rdata = pos + 10
            if rtype == 33:  # SRV: port + target host
                port = struct.unpack(">H", buf[rdata + 4 : rdata + 6])[0]
                target, _ = _read_name(buf, rdata + 6)
                ports[target] = port
            elif rtype == 1 and rdlen == 4:  # A
                addrs[name] = socket.inet_ntoa(buf[rdata : rdata + 4])
            pos = rdata + rdlen
    except (
        struct.error,
        IndexError,
        ValueError,
        OSError,
    ):  # OSError: inet_ntoa of a truncated A record
        return []
    return [f"http://{addrs[h]}:{p}" for h, p in ports.items() if h in addrs]


def mdns_browse(timeout: float = 1.5) -> list[str]:
    """One multicast PTR query; HA instances that answer within `timeout` seconds IN TOTAL (a host that keeps
    sending datagrams to the query's port cannot stretch it). [] on any network error."""
    found: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    deadline = time.monotonic() + timeout
    with contextlib.closing(sock):
        try:
            sock.sendto(_query(), ("224.0.0.251", 5353))
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                data, _ = sock.recvfrom(9000)
                found += [u for u in parse_mdns(data) if u not in found]
        except (TimeoutError, OSError):
            pass
    return found


def detect_ha(
    *,
    env: Mapping[str, str],
    hosts: Iterable[str] = (),
    fetch: Fetch = http_get,
    mdns: Callable[[], list[str]] = mdns_browse,
) -> list[HaFound]:
    """Every Home Assistant that answers; the caller asks the user which one (never auto-picked)."""
    cfg = load_ha_config()
    candidates: list[tuple[str, str]] = [
        (str(u), "configured")
        for u in (env.get("HA_URL"), env.get("PRINTOPS_HA_URL"), cfg.get("url"))
        if u
    ]
    candidates += [(u, "mdns") for u in mdns()]
    candidates += [
        (f"http://{h}:{HA_PORT}", via)
        for h, via in (
            ("homeassistant.local", "known name"),
            ("homeassistant", "known name"),
            ("localhost", "known name"),
            *((h, "spoolman host") for h in hosts),
        )
    ]
    seen: set[str] = set()
    out: list[HaFound] = []
    for url, via in candidates:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        if is_home_assistant(key, fetch):
            out.append(HaFound(key, via))
    return out
