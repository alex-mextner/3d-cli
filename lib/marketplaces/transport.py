"""HTTP transports for the marketplace clients.

`fetch` is plain stdlib urllib: enough for the public APIs (Printables GraphQL, Thingiverse
REST, Cults GraphQL) and the file CDNs. `fetch_impersonated` is for hosts behind Cloudflare
bot management that reject urllib's TLS fingerprint (MakerWorld): it sends the request with a
real Chrome TLS/HTTP2 fingerprint through curl-impersonate, either as the `curl_chromeNNN`
binary (the build that the axe repo's `tooling/curl-impersonate-patched` pins and exports
as AXE_CURL_IMPERSONATE_DIR) or through
the curl_cffi Python binding of the same library. Neither lane solves a JavaScript or
Turnstile challenge: a site that still answers "Just a moment..." is reported as blocked.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import UsageError
from marketplaces.base import MarketplaceError, as_str, dict_list, require_dict

USER_AGENT = "3d-cli (+https://github.com/alex-mextner/3d-cli)"
TIMEOUT_S = 30
# curl-impersonate wrapper names, newest first; axe pins chrome146.
CURL_IMPERSONATE_NAMES = ("curl_chrome146", "curl_chrome136", "curl_chrome131", "curl_chrome124",
                          "curl_chrome120", "curl_chrome116")
_CHALLENGE_MARKERS = (b"Just a moment...", b"challenges.cloudflare.com", b"cf-chl", b"__cf_chl")
# Sent to the URL's own host only: urllib copies headers onto redirects, and a file link that
# redirects to a CDN must not carry the user's token there.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def is_challenge(response: Response) -> bool:
    """A Cloudflare interstitial (JS/Turnstile challenge) instead of the real page."""
    return response.status in (403, 429, 503) and any(m in response.body for m in _CHALLENGE_MARKERS)


def _split(site: str, url: str) -> urllib.parse.SplitResult:
    try:
        return urllib.parse.urlsplit(url)
    except ValueError as exc:  # e.g. "http://[x": a hostile or broken link from site data
        raise MarketplaceError(site, "parse", f"malformed URL from the site: {url[:120]}") from exc


def same_host(url: str, other: str) -> bool:
    """Whether two URLs share scheme and host (a malformed one shares none): an http:// link
    on the API host must not get the token meant for https://."""
    try:
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(other)
    except ValueError:
        return False
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect to ftp://; site links stay on http(s) all the way."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise urllib.error.HTTPError(req.full_url, code, f"refusing a redirect to {newurl[:120]}",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def _request(site: str, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
             data: bytes | None = None) -> urllib.request.Request:
    """A urllib request for an http(s) URL from site data (never file:// or ftp://), with
    credential headers pinned to the first host."""
    if _split(site, url).scheme not in ("http", "https"):
        raise MarketplaceError(site, "parse", f"refusing a non-HTTP URL from the site: {url[:120]}")
    merged = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=data, method=method, headers={
        k: v for k, v in merged.items() if k.lower() not in _CREDENTIAL_HEADERS})
    for key, value in merged.items():
        if key.lower() in _CREDENTIAL_HEADERS:
            request.add_unredirected_header(key, value)
    return request


def fetch(
    site: str,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> Response:
    """One request through urllib. HTTP error statuses come back as a Response (callers map
    them to a reason); only transport failures raise, always as MarketplaceError."""
    request = _request(site, url, method=method, data=data,
                       headers={"Accept": "application/json", **(headers or {})})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as reply:
            return Response(reply.status, reply.read(), reply.geturl())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, _error_body(exc), url)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MarketplaceError(site, "network", f"cannot reach {url}: {reason}",
                               remediation=["Check the network connection and retry."]) from exc


def _error_body(exc: urllib.error.HTTPError) -> bytes:
    """The body of an HTTP error answer, or b"" when even that read fails (short body,
    timeout): the status alone still maps to a structured error."""
    try:
        return exc.read()
    except (OSError, http.client.HTTPException, ValueError):
        return b""


def graphql(site: str, url: str, query: str, variables: Mapping[str, Any], *,
            headers: Mapping[str, str] | None = None, auth_hint: str = "") -> dict[str, Any]:
    """POST a GraphQL query; returns `data`, raising on HTTP errors and on `errors`."""
    response = post_json(site, url, {"query": query, "variables": dict(variables)}, headers=headers)
    raise_for_status(site, response, auth_hint=auth_hint)
    payload = require_dict(site, decode_json(site, response), "response")
    errors = dict_list(payload.get("errors"))
    if errors:
        raise MarketplaceError(site, "http", "GraphQL error: "
                               + "; ".join(as_str(e.get("message")) for e in errors))
    return require_dict(site, payload.get("data"), "data")


def post_json(site: str, url: str, payload: Mapping[str, Any],
              headers: Mapping[str, str] | None = None) -> Response:
    body = json.dumps(payload).encode("utf-8")
    return fetch(site, url, method="POST", data=body,
                 headers={"Content-Type": "application/json", **(headers or {})})


def decode_json(site: str, response: Response) -> Any:
    try:
        return json.loads(response.body)
    except ValueError as exc:  # JSONDecodeError, or a body that is not even UTF-8
        raise MarketplaceError(site, "parse", f"{response.url} did not return JSON "
                               f"(HTTP {response.status})") from exc


def raise_for_status(site: str, response: Response, *, auth_hint: str = "") -> None:
    """Map a non-2xx answer to the matching MarketplaceError kind."""
    if 200 <= response.status < 300:
        return
    if is_challenge(response):
        raise MarketplaceError(site, "blocked",
                               f"Cloudflare bot challenge at {response.url} (HTTP {response.status})",
                               remediation=["The site wants a real browser; retry later or open the "
                                            "URL in a browser."])
    if response.status in (401, 403) and auth_hint:
        raise MarketplaceError(site, "auth", f"HTTP {response.status} from {response.url}: "
                               f"{_snippet(response)}", remediation=[auth_hint])
    if response.status == 404:
        raise MarketplaceError(site, "not-found", f"{response.url} not found (HTTP 404)")
    raise MarketplaceError(site, "http", f"HTTP {response.status} from {response.url}: "
                           f"{_snippet(response)}")


def _snippet(response: Response) -> str:
    return " ".join(response.text()[:160].split())


# ---------------------------------------------------------------------------------------
# TLS-impersonating lane (Cloudflare bot management keyed on the TLS/HTTP2 fingerprint)
# ---------------------------------------------------------------------------------------

def curl_impersonate_binary() -> str | None:
    """The curl-impersonate wrapper to use: $THREED_CURL_IMPERSONATE (an explicit binary),
    then axe's pinned build in $AXE_CURL_IMPERSONATE_DIR, then any curl_chromeNNN on PATH."""
    explicit = os.environ.get("THREED_CURL_IMPERSONATE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise UsageError(
                f"THREED_CURL_IMPERSONATE is not an executable file: {explicit}",
                command="models",
                remediation=["Point THREED_CURL_IMPERSONATE at a curl-impersonate wrapper "
                             "(curl_chrome146), or unset it to search PATH and AXE_CURL_IMPERSONATE_DIR."])
        return str(path)
    axe_dir = os.environ.get("AXE_CURL_IMPERSONATE_DIR", "").strip()
    if axe_dir:
        for name in CURL_IMPERSONATE_NAMES:
            candidate = Path(axe_dir) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    for name in CURL_IMPERSONATE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch_impersonated(site: str, url: str, *, headers: Mapping[str, str] | None = None) -> Response:
    """GET `url` (http/https only) with a Chrome TLS fingerprint: through a curl-impersonate binary when one is
    found, else through impersonate_fetch.py (curl_cffi) run by cli.pyrun. Both write the
    body to a file and print "<status> <final url>"; request headers (which may carry a
    token) go through a file in a private 0700 temp dir, never through argv."""
    if _split(site, url).scheme not in ("http", "https"):
        raise MarketplaceError(site, "parse", f"refusing a non-HTTP URL from the site: {url[:120]}")
    merged = {"Accept": "application/json", **(headers or {})}
    # With a token aboard, a redirect is not followed at all (it surfaces as a 3xx error):
    # unlike the urllib lane we cannot pin headers to the first host inside curl.
    follow = not any(k.lower() in _CREDENTIAL_HEADERS for k in merged)
    binary = curl_impersonate_binary()
    with tempfile.TemporaryDirectory(prefix="3d-models-") as tmp:
        out = Path(tmp) / "body"
        header_file = Path(tmp) / "headers"
        header_file.write_text("".join(f"{k}: {v}\n" for k, v in merged.items()), encoding="utf-8")
        if binary:
            argv = [binary, "-sS", *(["-L"] if follow else []), "--max-time", str(TIMEOUT_S),
                    "-o", str(out), "-H", f"@{header_file}", "-w", "%{http_code} %{url_effective}", url]
            tool = Path(binary).name
        else:
            from cli.pyrun import tool_argv

            argv = tool_argv("curl_cffi", "marketplaces/impersonate_fetch.py",
                             [url, str(out), str(header_file), "follow" if follow else "no-follow"])
            tool = "curl_cffi"
        try:
            # The first curl_cffi run lets uv fetch the wheel, hence the generous timeout.
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MarketplaceError(site, "network", f"{tool} failed for {url}: {exc}") from exc
        status_text, _, final_url = (proc.stdout.strip().splitlines() or [""])[-1].partition(" ")
        if proc.returncode != 0 or not status_text.isdecimal():
            detail = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
            raise MarketplaceError(site, "network", f"{tool} could not fetch {url}: {detail[:200]}")
        body = out.read_bytes() if out.exists() else b""
    return Response(int(status_text), body, final_url or url)


# ---------------------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------------------

def download(site: str, url: str, dest: Path, *, headers: Mapping[str, str] | None = None) -> int:
    """Stream `url` into `dest`; returns bytes written. On any failure `dest` is removed and
    a MarketplaceError raised (the caller stages downloads in a scratch dir). A body shorter
    than its Content-Length is a failure too: http.client ends the read silently when the
    connection drops, which would otherwise keep a truncated file."""
    request = _request(site, url, headers=headers)
    written = 0
    try:
        with dest.open("wb") as handle, _OPENER.open(request, timeout=TIMEOUT_S) as reply:
            while chunk := reply.read(1 << 16):
                handle.write(chunk)
                written += len(chunk)
            expected = (reply.headers.get("Content-Length") or "").strip()
            if expected.isdecimal() and written != int(expected):
                raise MarketplaceError(site, "network", f"download truncated for {url}: got "
                                       f"{written} of {expected} bytes",
                                       remediation=["The connection dropped; retry the download."])
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        with_credentials = any(k.lower() in _CREDENTIAL_HEADERS for k in (headers or {}))
        hint = (f"{site} refused the stored credentials for this file; check which source holds "
                "them with `3d models auth status`." if with_credentials else "")
        raise_for_status(site, Response(exc.code, _error_body(exc), url), auth_hint=hint)
        raise  # unreachable: an HTTPError is never 2xx
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        dest.unlink(missing_ok=True)
        raise MarketplaceError(site, "network", f"download failed for {url}: "
                               f"{getattr(exc, 'reason', exc)}") from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return written
