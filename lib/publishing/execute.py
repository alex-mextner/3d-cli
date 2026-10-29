"""Carry out a `PlatformPlan` for real (`3d models publish --yes`).

* Thingiverse: the REST API through `marketplaces.transport`, with the OAuth token from
  `marketplaces.credentials.thingiverse_upload_token()` (env / keychain / auth.json, never the
  repo). A failure after the draft exists leaves an unpublished draft and says where.
* MakerWorld, Printables: agent-browser drives the website in its own headed, restorable
  session `3d-publish-<platform>-<hash of title and files>` (one per publication). A login wall is passed with `keychain fill` (the value goes
  keychain -> browser, this process never sees it); 2FA, captcha and the final Publish click
  stay with the owner, and so does any step the site's page no longer answers: the run stops
  there, leaves the window open and says what is left.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from marketplaces import thingiverse, transport
from marketplaces.base import MarketplaceError, as_int, as_str
from publishing.plan import AGENT_BROWSER, PlatformPlan
from publishing.source import split_url

SITE = "thingiverse"
LOGIN_HINTS = ("login", "signin", "sign-in", "sign_in", "/auth")
KEYCHAIN_CALLER = "3d-cli"
USERNAME_SELECTOR = "input[autocomplete=username], input[type=email], input[name=email]"
Fetch = Callable[..., transport.Response]
Runner = Callable[
    [Sequence[str]], tuple[int, str]
]  # argv -> (exit code, stdout + stderr)


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----3d-publish-{uuid.uuid4().hex}"
    safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; "
        f'name="{field}"; filename="{safe}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return (
        head + content + f"\r\n--{boundary}--\r\n".encode(),
        f"multipart/form-data; boundary={boundary}",
    )


def _json(response: transport.Response, what: str) -> dict[str, Any]:
    transport.raise_for_status(
        SITE,
        response,
        auth_hint="Check the Thingiverse OAuth access token (write scope): `3d models auth set thingiverse-upload`.",
    )
    payload = transport.decode_json(SITE, response)
    if not isinstance(payload, dict):
        raise MarketplaceError(
            SITE, "parse", f"unexpected answer to {what}: not an object"
        )
    return payload


def _post_json(
    fetch: Fetch, url: str, payload: dict[str, Any], headers: dict[str, str]
) -> transport.Response:
    """POST a JSON body through the injected `fetch`, never the global transport."""
    return fetch(
        SITE,
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )


def publish_thingiverse(
    plan: PlatformPlan,
    *,
    token: str,
    api: str | None = None,
    fetch: Fetch = transport.fetch,
) -> str:
    """Create the thing, upload files and images, finalize them, publish. Returns the thing URL."""
    base = (api or thingiverse.api_url()).rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}
    created = _json(
        _post_json(fetch, f"{base}/things", plan.fields, auth),
        "POST /things",
    )
    thing_id = as_int(created.get("id"))
    if thing_id is None:
        raise MarketplaceError(SITE, "parse", "POST /things answered without an id")
    web = f"{thingiverse.WEB_URL}/thing:{thing_id}"
    try:
        pending: list[dict[str, int]] = []
        for rank, path in enumerate((*plan.files, *plan.images), start=1):
            body, content_type = _multipart("file", path.name, path.read_bytes())
            reply = fetch(
                SITE,
                f"{base}/files/0/uploadFile",
                method="POST",
                data=body,
                headers={**auth, "Content-Type": content_type},
            )
            uploaded = as_int(_json(reply, f"upload of {path.name}").get("id"))
            if uploaded is None:
                raise MarketplaceError(
                    SITE, "parse", f"the upload of {path.name} answered without an id"
                )
            pending.append({"id": uploaded, "rank": rank * 10})
        finalize = {
            "pending_uploads": pending,
            "target_id": thing_id,
            "target_type": "thing",
        }
        _json(
            _post_json(fetch, f"{base}/files/0/FinalizeFiles", finalize, auth),
            "FinalizeFiles",
        )
        published = _json(
            fetch(
                SITE, f"{base}/things/{thing_id}/publish", method="POST", headers=auth
            ),
            "publish",
        )
        errors = published.get("errors")
        if errors:
            raise MarketplaceError(
                SITE, "http", f"Thingiverse refused to publish: {errors}"
            )
    except (MarketplaceError, OSError) as exc:
        kind = exc.kind if isinstance(exc, MarketplaceError) else "parse"
        reason = (
            exc.reason
            if isinstance(exc, MarketplaceError)
            else f"cannot read an upload file: {exc}"
        )
        remediation = exc.remediation if isinstance(exc, MarketplaceError) else []
        raise MarketplaceError(
            SITE,
            kind,
            f"{reason} (thing {thing_id} stays an unpublished draft)",
            remediation=[*remediation, f"Finish or delete the draft at {web}."],
        ) from exc
    return as_str(published.get("public_url")) or web


@dataclass(frozen=True)
class BrowserOutcome:
    ok: bool
    session: str
    message: str


BROWSER_TIMEOUT = 180
KEYCHAIN_APPROVAL_TIMEOUT = (
    330  # keychain waits up to 300 s for the owner's approval of a fill
)


def run_agent_browser(argv: Sequence[str]) -> tuple[int, str]:
    limit = KEYCHAIN_APPROVAL_TIMEOUT if argv[0] == "keychain" else BROWSER_TIMEOUT
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=limit,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out after {limit} s"
    return done.returncode, (done.stdout + done.stderr).strip()


def _login(platform: str, base: list[str], runner: Runner) -> str | None:
    """Type the stored credentials into the login page; None on success, else why not."""
    code, cdp = runner([*base, "get", "cdp-url"])
    if code != 0 or not cdp.startswith("ws://"):
        return f"cannot reach the browser's DevTools endpoint: {cdp}"
    fills: tuple[tuple[str, str, list[str]], ...] = (
        (
            f"{KEYCHAIN_CALLER}/{platform}-user",
            USERNAME_SELECTOR,
            ["--field", "username"],
        ),
        (f"{KEYCHAIN_CALLER}/{platform}-password", "input[type=password]", []),
    )
    for ref, selector, extra in fills:
        code, out = runner(
            [
                "keychain",
                "fill",
                ref,
                "--cdp",
                cdp,
                "--selector",
                selector,
                *extra,
                "--caller",
                KEYCHAIN_CALLER,
            ]
        )
        if code != 0:
            return (
                f"keychain could not fill {ref} (exit {code}): {out}. Store the login with "
                f"`keychain set {ref} --origin https://<site>` and, if it was refused, add the grant it names"
            )
    return None


def _session_name(plan: PlatformPlan) -> str:
    """One session per publication: the title plus where its files live (two projects may share a title). A
    make post has neither: its identity is the model page it is posted on and the photos it attaches."""
    identity = "\0".join(
        [
            str(plan.fields.get("title", "")),
            str(plan.fields.get("model_url", "")),
            *(str(p.resolve()) for p in plan.files),
            *(str(p.resolve()) for p in plan.images if plan.fields.get("model_url")),
        ]
    )
    return f"3d-publish-{plan.platform}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"


def publish_browser(
    plan: PlatformPlan, runner: Runner = run_agent_browser
) -> BrowserOutcome:
    """Run the plan's agent-browser steps; stop at the first one the page does not answer.

    Every publication gets its own browser session (platform + title + file locations), so a draft awaiting its
    owner's final click is never navigated away by the next project; two runs of the same
    publication queue on a lock instead of interleaving their page navigation and form fills.
    """
    lock_path = Path(tempfile.gettempdir()) / f"{_session_name(plan)}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _publish_browser_locked(plan, runner)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)


def _login_outcome(
    plan: PlatformPlan,
    base: list[str],
    runner: Runner,
    landed: tuple[int, str] | None = None,
) -> BrowserOutcome | None:
    """When the page is a login wall: fill it (and stop for the owner), or say why not. `landed` is a `get url`
    answer the caller already has."""
    session = base[2]
    code, url = landed if landed is not None else runner([*base, "get", "url"])
    if code != 0 or not any(hint in url.lower() for hint in LOGIN_HINTS):
        return None
    problem = _login(plan.platform, base, runner)
    if problem is not None:
        return BrowserOutcome(False, session, problem)
    return BrowserOutcome(
        False,
        session,
        f"credentials typed into the {plan.platform} login page of browser session {session}: press Sign in, "
        "finish any 2FA/captcha yourself, then run the same command again (the login is kept)",
    )


def _wrong_page_outcome(
    plan: PlatformPlan, session: str, landed: tuple[int, str]
) -> BrowserOutcome | None:
    """A make post is attached to the model page it asked for: when the browser landed anywhere else (another
    model, another site, a page whose address cannot be read), refuse before any photo is uploaded."""
    wanted = str(plan.fields.get("model_url", ""))
    if not wanted:
        return None
    code, url = landed
    if code == 0 and _same_model_page(wanted, url):
        return None
    seen = url if code == 0 else f"an unreadable address ({url})"
    return BrowserOutcome(
        False,
        session,
        f"the browser is on {seen}, not the requested model page {wanted}: nothing was uploaded or filled. "
        "Check the model URL and run the command again",
    )


def _same_model_page(wanted: str, landed: str) -> bool:
    """Whether `landed` is the model page `wanted` names: the same site and model id. A site may redirect to the
    page's canonical slug or locale, so those parts are not compared; the query and fragment a page adds are
    ignored, but another model or another host is not the page."""
    site_and_id = split_url(wanted)
    page = landed.split("#", 1)[0].split("?", 1)[0]
    return site_and_id != ("", "") and split_url(page) == site_and_id


def _run_steps(
    plan: PlatformPlan, base: list[str], runner: Runner
) -> tuple[BrowserOutcome | None, str]:
    """Run the form steps after the first; the failure (if any) and the last step's output."""
    session, out = base[2], ""
    for number, step in enumerate(plan.browser_steps[1:], start=2):
        code, out = runner([*base, *step])
        if code != 0:
            login = _login_outcome(plan, base, runner)
            if login is not None:
                return (
                    login,
                    out,
                )  # the step bounced to a login wall: fill it, do not report a form error
            left = "; ".join(
                f"{k}: {v}" for k, v in plan.fields.items() if k not in ("remix_of",)
            )
            return (
                BrowserOutcome(
                    False,
                    session,
                    f"step {number} ({' '.join(step[:4])}) failed: {out}. The page may have changed; the window of "
                    f"session {session} stays open — finish the form by hand with: {left}",
                ),
                out,
            )
    return None, out


def _publish_browser_locked(plan: PlatformPlan, runner: Runner) -> BrowserOutcome:
    session = _session_name(plan)
    base = [AGENT_BROWSER, "--session", session]
    code, out = runner([*base, "--headed", "--restore", *plan.browser_steps[0]])
    if code != 0:
        return BrowserOutcome(
            False, session, f"could not open {plan.browser_steps[0][-1]}: {out}"
        )
    landed = runner([*base, "get", "url"])
    login = _login_outcome(plan, base, runner, landed)
    if login is not None:
        return login
    wrong = _wrong_page_outcome(plan, session, landed)
    if wrong is not None:
        return wrong
    failed, out = _run_steps(plan, base, runner)
    if failed is not None:
        return failed
    return BrowserOutcome(
        True,
        session,
        f"the {plan.platform} draft is filled in browser session {session} (screenshot: {out or 'taken'}). "
        "Review it and press Publish yourself.",
    )


CULTS = "cults"
CULTS_CATEGORIES = "query Categories { categories { id name(locale: EN) } }"
CULTS_LICENSES = "query Licenses { licenses { code name } }"
CULTS_CREATE = (
    "mutation Create($name: String!, $description: String!, $categoryId: ID!, $licenseCode: String!, "
    "$tagNames: [String!], $fileUrls: [String!], $imageUrls: [String!]) { createCreation(name: $name, "
    "description: $description, categoryId: $categoryId, licenseCode: $licenseCode, tagNames: $tagNames, "
    "fileUrls: $fileUrls, imageUrls: $imageUrls, locale: EN, downloadPrice: 0, currency: EUR, "
    "madeWithAi: false) { creation { id url(locale: EN) } errors } }"
)


def _cults_call(
    query: str, variables: dict[str, Any], auth: dict[str, str], api: str
) -> dict[str, Any]:
    return transport.graphql(
        CULTS,
        api,
        query,
        variables,
        headers=auth,
        auth_hint="Cults rejected the username/API key: `3d models auth set cults`.",
    )


def _cults_category(
    response: Mapping[str, Any], fields: Mapping[str, Any]
) -> Mapping[str, Any]:
    cats = response.get("categories") or []
    wanted = str(fields["category"]).strip().casefold()
    category = next(
        (c for c in cats if str(c.get("name", "")).strip().casefold() == wanted), None
    )
    if category is None:
        names = ", ".join(sorted(str(c.get("name")) for c in cats)[:20])
        raise MarketplaceError(
            CULTS,
            "parse",
            f"Cults has no category named {fields['category']!r}",
            remediation=[
                f"Set `category` in publish.yaml to one of: {names or '(the query returned none)'}"
            ],
        )
    return category


def _cults_licence(
    response: Mapping[str, Any], fields: Mapping[str, Any], license_id: str
) -> Mapping[str, Any]:
    from publishing.licenses import parse_license

    lics = response.get("licenses") or []
    licence = next(
        (
            lic
            for lic in lics
            if (parsed := parse_license(str(lic.get("name", "")))) is not None
            and parsed.id == license_id
        ),
        None,
    )
    if licence is None:
        offered = ", ".join(str(lic.get("name")) for lic in lics)
        raise MarketplaceError(
            CULTS,
            "parse",
            f"Cults does not offer a licence matching {fields['license']!r}",
            remediation=[
                f"Pick a publish.yaml `license` Cults offers: {offered or '(the query returned none)'}"
            ],
        )
    return licence


def publish_cults(
    plan: PlatformPlan,
    license_id: str,
    *,
    credentials: tuple[str, str],
    api: str | None = None,
) -> str:
    """Create the Cults3D creation from the plan: resolve the category and licence ids with two read-only
    queries, then `createCreation` with the hosted file/image URLs. Returns the creation URL.

    The shapes follow the community-documented API (github.com/CheekyCodexConjurer/cults3d-api-docs) and are
    NOT verified against a real account (#565); the loopback test uses fixtures of those shapes."""
    import base64

    from marketplaces import cults

    username, key = credentials
    basic = base64.b64encode(f"{username}:{key}".encode()).decode("ascii")
    auth = {"Authorization": f"Basic {basic}"}
    url = api or cults.api_url()
    fields = plan.fields
    category = _cults_category(_cults_call(CULTS_CATEGORIES, {}, auth, url), fields)
    licence = _cults_licence(
        _cults_call(CULTS_LICENSES, {}, auth, url), fields, license_id
    )
    variables = {
        "name": fields["name"],
        "description": fields["description"],
        "categoryId": category["id"],
        "licenseCode": licence["code"],
        "tagNames": list(fields["tags"]),
        "fileUrls": list(fields["file_urls"]),
        "imageUrls": list(fields["image_urls"]),
    }
    made = _cults_call(CULTS_CREATE, variables, auth, url).get("createCreation") or {}
    errors = made.get("errors")
    creation = made.get("creation") or {}
    if errors or not creation:
        raise MarketplaceError(
            CULTS,
            "http",
            f"Cults refused the creation: {errors or 'no creation returned'}",
        )
    return (
        as_str(creation.get("url"))
        or f"{cults.WEB_URL} (creation {creation.get('id')})"
    )
