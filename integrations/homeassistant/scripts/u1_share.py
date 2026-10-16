#!/usr/bin/env python3
"""Time-limited Home Assistant camera links. No inbound port or daemon required.

Only signs /api/camera_proxy/<camera entity>; never exports the HA access token.
Python >= 3.10; pip install -r requirements.txt
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import base64
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_TTL = 7 * 24 * 3600
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "u1-share"
CONFIG_FILE = CONFIG_DIR / "config.json"
ENTITY_RE = re.compile(r"camera\.[a-z0-9_]+\Z")


class ShareError(Exception):
    """Safe, user-facing errors: do not include tokens or signed URLs."""


def duration(text: str) -> int:
    """Parse 30s, 15m, 2h, 1h30m or integer seconds; limit accidental exposure."""
    text = text.strip().lower()
    if text.isascii() and text.isdigit():
        value = int(text)
    else:
        parts = list(re.finditer(r"([0-9]+)([smhd])", text))
        if not parts or "".join(p.group() for p in parts) != text:
            raise ShareError("Use a duration such as 30s, 15m, 2h, 1h30m or 1d.")
        value = sum(int(p[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[p[2]] for p in parts)
    if not 1 <= value <= MAX_TTL:
        raise ShareError("Duration must be between 1 second and 7 days.")
    return value


def origin(value: str, *, public: bool = False) -> str:
    """Require an origin, not an arbitrary URL or a redirect target."""
    if not isinstance(value, str) or any(c.isspace() or c == "\\" for c in value):
        raise ShareError("Invalid Home Assistant URL.")
    try:
        p = urlsplit(value)
        host, port = p.hostname, p.port
    except ValueError:
        raise ShareError("Invalid Home Assistant URL.") from None
    if (p.scheme not in ("http", "https") or not host or p.username is not None
            or p.password is not None or p.query or p.fragment or p.path not in ("", "/")):
        raise ShareError("Use only the HA origin, e.g. https://ha.example.com, without a path or credentials.")
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
        raise ShareError("Use an ASCII hostname (IDN domains must be in punycode).")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if public:
        if p.scheme != "https":
            raise ShareError("Public links require HTTPS; use your existing external HA URL.")
        if (ip and not ip.is_global) or host.lower() == "localhost" or host.lower().endswith((".local", ".localhost")) or "." not in host:
            raise ShareError("The public URL must not be a LAN, loopback or Tailscale IP.")
    elif p.scheme == "http":
        # This permits the verified Tailscale IP without weakening TLS for public URLs.
        if not ip or not (ip.is_loopback or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))):
            raise ShareError("Use HTTPS, a loopback address, or the HA Tailscale IP for authentication.")
    netloc = f"[{host.lower()}]" if ":" in host else host.lower()
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((p.scheme, netloc, "", "", ""))


def websocket_url(base: str) -> str:
    p = urlsplit(origin(base))
    return urlunsplit(("wss" if p.scheme == "https" else "ws", p.netloc, "/api/websocket", "", ""))


class HA:
    """Small synchronous HA WebSocket client with timeouts and redacted errors."""
    def __init__(self, base: str, token: str, timeout: float = 15):
        self.uri, self.token, self.timeout = websocket_url(base), token, timeout
        self.ws: Any = None
        self._stack = ExitStack()
        self.seq = 0

    def __enter__(self) -> HA:
        try:
            from websockets.sync.client import connect
        except ImportError:
            raise ShareError("Install dependencies: python3 -m pip install -r requirements.txt") from None
        try:
            self.ws = self._stack.enter_context(connect(
                self.uri, proxy=None, open_timeout=self.timeout,
                close_timeout=2, max_size=16 * 1024 * 1024))
            if self._receive().get("type") != "auth_required":
                raise ShareError("The endpoint did not return the HA authentication handshake.")
            self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if self._receive().get("type") != "auth_ok":
                raise ShareError("HA authentication failed. Check your locally stored access token.")
            return self
        except Exception as e:
            self._stack.close()
            if isinstance(e, ShareError):
                raise
            raise ShareError(f"HA connection failed ({type(e).__name__}); check the URL and Tailscale.") from None

    def __exit__(self, *_: Any) -> None:
        self._stack.close()

    def _receive(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            data = json.loads(self.ws.recv(timeout=self.timeout if timeout is None else timeout))
        except Exception as e:
            raise ShareError(f"HA response failed ({type(e).__name__}).") from None
        if not isinstance(data, dict):
            raise ShareError("Unexpected HA response format.")
        return data

    def call(self, command: str, **data: Any) -> Any:
        self.seq += 1
        try:
            self.ws.send(json.dumps({"id": self.seq, "type": command, **data}))
        except Exception as e:
            raise ShareError(f"HA request failed ({type(e).__name__}).") from None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            msg = self._receive(max(0.001, deadline - time.monotonic()))
            if msg.get("id") != self.seq:
                continue
            if msg.get("type") != "result" or not msg.get("success"):
                # Server error text may contain private configuration; don't echo it.
                raise ShareError(f"HA rejected {command}. Check entity permissions and integration availability.")
            return msg.get("result")
        raise ShareError("HA request timed out.")


def camera_states(ha: HA) -> list[dict[str, str]]:
    result = []
    for s in ha.call("get_states"):
        entity = s.get("entity_id", "")
        if ENTITY_RE.fullmatch(entity):
            result.append({"entity_id": entity, "name": str(s.get("attributes", {}).get("friendly_name", entity)),
                           "state": s.get("state", "unknown")})
    return sorted(result, key=lambda s: s["entity_id"])


def discover_public_url(ha: HA) -> str | None:
    config = ha.call("get_config")
    value = config.get("external_url")
    if value:
        try:
            return origin(value, public=True)
        except ShareError:
            pass
    try:
        cloud = ha.call("cloud/status")
    except ShareError:
        return None
    domain = cloud.get("remote_domain")
    if cloud.get("remote_connected") and isinstance(domain, str):
        try:
            return origin("https://" + domain, public=True)
        except ShareError:
            pass
    return None


def signed_metadata(path: str, entity: str, started: int, ttl: int) -> int:
    """Validate returned scope; decode exp only for display, never for authorization."""
    p = urlsplit(path)
    if p.scheme or p.netloc or p.fragment or p.path != "/api/camera_proxy/" + entity:
        raise ShareError("HA returned an unexpected signed path.")
    params = parse_qs(p.query, strict_parsing=True)
    if set(params) != {"authSig"} or len(params["authSig"]) != 1 or not params["authSig"][0]:
        raise ShareError("HA returned an invalid camera signature.")
    try:
        payload = params["authSig"][0].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims["exp"]
        if type(exp) is not int or exp <= 0:
            raise ValueError()
        return exp
    except (ValueError, KeyError, IndexError, TypeError):
        # Future HA versions may use an opaque signature; fail early in the viewer.
        return started + ttl


def generate(ha: HA, entity: str, public_url: str, ttl: int, refresh: int = 5) -> dict[str, Any]:
    if not ENTITY_RE.fullmatch(entity):
        raise ShareError("Select an actual camera.* entity, not a stream URL or dashboard path.")
    if not 1 <= ttl <= MAX_TTL or not 2 <= refresh <= 60:
        raise ShareError("TTL must be 1s–7d and refresh must be 2–60 seconds.")
    public_url = origin(public_url, public=True)
    states = {s["entity_id"]: s for s in camera_states(ha)}
    if entity not in states:
        raise ShareError("Camera entity not found; run the inspect command to list cameras.")
    if states[entity]["state"] in ("unavailable", "unknown", "off"):
        raise ShareError("The camera is unavailable/off. Fix its HA integration before sharing it.")
    started = int(time.time())
    path = ha.call("auth/sign_path", path="/api/camera_proxy/" + entity, expires=ttl)["path"]
    exp = signed_metadata(path, entity, started, ttl)
    data = {"v": 1, "path": path, "exp": exp, "refresh": refresh}
    viewer = public_url + "/local/u1-share/viewer.html#" + quote(json.dumps(data, separators=(",", ":")), safe="")
    return {"entity_id": entity, "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
            "expires_unix": exp, "ttl_seconds": ttl, "image_url": public_url + path, "viewer_url": viewer,
            "note": "Viewer requires www/u1-share files in HA. HA restart or issuer revocation invalidates links early."}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


def probe_image(url: str) -> dict[str, Any]:
    """Anonymous fetch; no cookies, Bearer token, redirects, or ambient proxies."""
    request = Request(url, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            signature = response.read(16)
            valid_image = (content_type == "image/jpeg" and signature.startswith(b"\xff\xd8\xff")) or (
                content_type == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n")) or (
                content_type == "image/webp" and signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
            return {"ok": response.status == 200 and valid_image, "status": response.status,
                    "content_type": content_type, "scope": "generator_network_only"}
    except HTTPError as e:
        return {"ok": False, "status": e.code, "scope": "generator_network_only"}
    except (URLError, TimeoutError, OSError):
        return {"ok": False, "status": None, "scope": "generator_network_only"}


def read_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        value = json.loads(CONFIG_FILE.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ShareError("Cannot read ~/.config/u1-share/config.json.") from None


def read_token(config: dict[str, Any]) -> str:
    token = os.environ.get("HA_TOKEN")
    if not token:
        path = Path(os.environ.get("HA_TOKEN_FILE", config.get("token_file", str(CONFIG_DIR / "token")))).expanduser()
        if path.exists():
            try:
                if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise ShareError("Token file is not private. Set its permissions to 600.")
                token = path.read_text().strip()
            except OSError:
                raise ShareError("Cannot read the local HA token file.") from None
    if not token:
        raise ShareError("Run setup, or set HA_TOKEN / HA_TOKEN_FILE locally. Do not paste your token into chat.")
    return token.strip()


def private_write(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def setup(base: str | None) -> None:
    if not sys.stdin.isatty():
        raise ShareError("Run setup interactively in your own terminal.")
    if CONFIG_FILE.exists() or (CONFIG_DIR / "token").exists():
        raise ShareError("Configuration already exists; edit the local config rather than overwriting credentials.")
    base = origin(base or input("HA URL (HTTPS or Tailscale IP): ").strip())
    token = getpass.getpass("HA long-lived access token (hidden): ").strip()
    if not token:
        raise ShareError("An access token is required.")
    with HA(base, token) as ha:
        cameras = camera_states(ha)
        if not cameras:
            raise ShareError("No camera entities found. Integrate the U1 camera into HA first.")
        for i, camera in enumerate(cameras, 1):
            print(f"{i}. {camera['entity_id']} — {camera['name']} ({camera['state']})")
        choice = input("Number of the U1 camera: ").strip()
        if not choice.isascii() or not choice.isdigit() or not 1 <= int(choice) <= len(cameras):
            raise ShareError("Invalid camera selection.")
        candidate = discover_public_url(ha) or ""
        public_url = origin(input(f"Public HTTPS HA URL [{candidate}]: ").strip() or candidate, public=True)
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(CONFIG_DIR / "token", token + "\n")
    try:
        private_write(CONFIG_FILE, json.dumps({"ha_url": base, "public_url": public_url,
            "entity": cameras[int(choice) - 1]["entity_id"], "token_file": str(CONFIG_DIR / "token")}, indent=2) + "\n")
    except Exception:
        (CONFIG_DIR / "token").unlink()  # Roll back only the token created by this invocation.
        raise
    print("Saved private configuration. Generate a link with: python3 u1_share.py --ttl 2h")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("share", "inspect", "setup"), default="share")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL"))
    parser.add_argument("--public-url", default=os.environ.get("HA_PUBLIC_URL"))
    parser.add_argument("--entity", default=os.environ.get("HA_CAMERA"))
    parser.add_argument("--ttl", default="1h", help="e.g. 15m, 2h, 1h30m, 1d; maximum 7d")
    parser.add_argument("--refresh", type=int, default=5, help="viewer interval, 2–60 seconds")
    parser.add_argument("--format", choices=("image", "viewer", "json"), default="image")
    parser.add_argument("--verify", action="store_true", help="anonymously test the image URL; not proof of internet reachability")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            setup(args.ha_url)
            return 0
        config = read_config()
        base = args.ha_url or config.get("ha_url")
        if not base:
            raise ShareError("Set HA_URL or --ha-url, or run setup.")
        ttl = duration(args.ttl)
        with HA(origin(base), read_token(config)) as ha:
            if args.command == "inspect":
                print(json.dumps({"cameras": camera_states(ha), "detected_public_url": discover_public_url(ha)}, indent=2, ensure_ascii=False))
                return 0
            public_url = args.public_url or config.get("public_url") or discover_public_url(ha)
            entity = args.entity or config.get("entity")
            if not public_url or not entity:
                raise ShareError("Specify --entity and --public-url, or run setup / inspect.")
            result = generate(ha, entity, public_url, ttl, args.refresh)
        if args.verify:
            result["verification"] = probe_image(result["image_url"])
            if not result["verification"]["ok"]:
                raise ShareError(f"Anonymous image check failed (HTTP {result['verification']['status']}). No link printed; check camera, external URL and access gates.")
            print("Anonymous image check passed from this machine, not independently from the public internet.", file=sys.stderr)
        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            print(result["viewer_url" if args.format == "viewer" else "image_url"])
            print(f"Expires at {result['expires_at']} (or earlier after HA restart/token revocation).", file=sys.stderr)
        return 0
    except (ShareError, OSError) as e:
        print(f"u1-share: {str(e) if isinstance(e, ShareError) else type(e).__name__}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError):
        print("u1-share: Unexpected HA response or invalid configuration; no secret details printed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("u1-share: Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
