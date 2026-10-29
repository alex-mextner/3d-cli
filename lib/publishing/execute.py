"""Carry out a `PlatformPlan` for real (`3d models publish --yes`).

* Thingiverse: the REST API through `marketplaces.transport`, with the OAuth token from
  `marketplaces.credentials.thingiverse_upload_token()` (env / keychain / auth.json, never the
  repo). A failure after the draft exists leaves an unpublished draft and says where.
* MakerWorld, Printables: agent-browser drives the website in its own headed, restorable
  session `3d-publish-<platform>-<title hash>` (one per publication). A login wall is passed with `keychain fill` (the value goes
  keychain -> browser, this process never sees it); 2FA, captcha and the final Publish click
  stay with the owner, and so does any step the site's page no longer answers: the run stops
  there, leaves the window open and says what is left.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import subprocess
import tempfile
import uuid
from pathlib import Path
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from marketplaces import thingiverse, transport
from marketplaces.base import MarketplaceError, as_int, as_str
from publishing.plan import AGENT_BROWSER, PlatformPlan

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
        transport.post_json(SITE, f"{base}/things", plan.fields, headers=auth),
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
            transport.post_json(
                SITE, f"{base}/files/0/FinalizeFiles", finalize, headers=auth
            ),
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
        reason = exc.reason if isinstance(exc, MarketplaceError) else f"cannot read an upload file: {exc}"
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


def run_agent_browser(argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=180, check=False
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out after 180 s"
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
    title = str(plan.fields.get("title", ""))
    return f"3d-publish-{plan.platform}-{hashlib.sha256(title.encode()).hexdigest()[:8]}"


def publish_browser(
    plan: PlatformPlan, runner: Runner = run_agent_browser
) -> BrowserOutcome:
    """Run the plan's agent-browser steps; stop at the first one the page does not answer.

    Every publication gets its own browser session (platform + title), so a draft awaiting its
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


def _login_outcome(plan: PlatformPlan, base: list[str], runner: Runner) -> BrowserOutcome | None:
    """When the page is a login wall: fill it (and stop for the owner), or say why not."""
    session = base[2]
    code, url = runner([*base, "get", "url"])
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
                return login, out  # the step bounced to a login wall: fill it, do not report a form error
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
    login = _login_outcome(plan, base, runner)
    if login is not None:
        return login
    failed, out = _run_steps(plan, base, runner)
    if failed is not None:
        return failed
    return BrowserOutcome(
        True,
        session,
        f"the {plan.platform} draft is filled in browser session {session} (screenshot: {out or 'taken'}). "
        "Review it and press Publish yourself.",
    )
