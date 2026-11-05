"""force_hatch.py — the one-time Telegram approval behind `3d printer start --force`.

WHAT: `--force` used to start a print through preflight FAILs by itself. An agent could (and did,
  2026-10-22: a spoon on the K1 SE, two false-positive FAILs) pass it on its own. Now `--force`
  only *asks to override*: the override happens when the owner approves a hatch request on
  Telegram. The request carries the failing checks and the written justification.

HOW: the same shared hatch helper the agent-hooks and `gh ship --skip-ci` use
  (agent-tools `lib/agenttools_hatch_escalation`, `request_hatch_approval`): the justification is
  in the env var `RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE="<why>"` (hook id
  `3d-printer-start-force`); with it set the helper sends one `tg-ctl ask` and blocks for the
  human's tap (a critical ask: no local clock; a late Approve is a grant a retry redeems with
  `RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE=approved:hgr-…`). Without it, or on deny / timeout /
  no tg-ctl / no helper, the start is refused. There is no self-service override.

WHY it is not a flag: a FAIL is either a real defect (do not print) or a false positive of a gate
  (fix the gate). Each approved or refused force is therefore an issue to analyse and a root cause to
  fix, not a habit — the refusal message says so.

RETRY (issue https://github.com/alex-mextner/3d-cli/issues/728): the tg-ctl daemon answers a card it
  could not send with `ask-forward not-posted: … daemon declined: reason=send-failed` (tg-cli
  `forwardAndPostQuestion`: ONE `sendMessage`, no retry; on 2026-11-04 22:02 and 22:03 it was
  `getaddrinfo EAI_AGAIN api.telegram.org`, a resolver blip, while a plain `tg` a moment later
  worked). Nothing was posted, so asking again is safe: the ask is repeated after each pause of
  RETRY_DELAYS_S (3, 10, 30 s). When it still fails the refusal says how to start from the
  printer's own screen and where the daemon logs the cause.

INVARIANTS: the helper is loaded from a FIXED path under the account's real home (never an env var,
  never the repo or $PATH), and only when its file is owned by you or root and not group/world
  writable; a helper that is missing or untrusted refuses the start (fail closed).
"""

from __future__ import annotations

import functools
import importlib.util
import os
import pwd
import shlex
import stat
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from printer_ops.base import PrinterRefused
from printer_ops.preflight import FAIL, Check

HOOK_ID = "3d-printer-start-force"
ENV_VAR = "RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE"

_MODULE = "agenttools_hatch_escalation"
# agent-tools checkouts searched, relative to the real home; the first one that has the helper wins.
_CHECKOUTS = (Path("dev") / "agent-tools", Path("xp") / "agent-tools")
_DETAIL_CHARS = 240
_CARD_VALUE_CHARS = (
    1000  # the shared hatch helper cuts each context value at this length
)
# The daemon's own wording for a card it could not post (agent-tools `_ask_forward_verdict`, tg-cli
# question-cards.ts `dropAsk(socket, "send-failed")`).
_SEND_FAILED = "reason=send-failed"
# Pauses before each further ask while the card is not posted: a transient resolver/network failure
# of the daemon clears in seconds, so the owner still gets the card and the start does not stall.
RETRY_DELAYS_S = (3.0, 10.0, 30.0)


class _Verdict(Protocol):
    approved: bool
    should_stop: bool
    reason: str


class _HatchLib(Protocol):
    def request_hatch_approval(
        self,
        hook_id: str,
        context: dict[str, object],
        *,
        cwd: str,
        command: str,
        critical: bool,
    ) -> _Verdict: ...


class HatchUnavailable(RuntimeError):
    """The shared hatch helper cannot be loaded from a trusted place."""


def _untrusted(path: Path) -> str | None:
    """Why `path` may not be trusted (owner other than you/root, group/world-writable), else None."""
    info = path.stat()
    if info.st_uid not in (0, os.getuid()):
        return f"{path} is owned by uid {info.st_uid}"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} is writable by group/others"
    return None


@functools.cache
def load_hatch() -> _HatchLib:
    """The shared helper, loaded from its exact file (not by name off sys.path)."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise HatchUnavailable(f"real home directory unknown: {exc}") from exc
    problems: list[str] = []
    for checkout in _CHECKOUTS:
        init = home / checkout / "lib" / _MODULE / "__init__.py"
        if not init.is_file():
            problems.append(f"{init}: not found")
            continue
        bad = _untrusted(init) or _untrusted(init.parent)
        if bad:
            problems.append(bad)
            continue
        spec = importlib.util.spec_from_file_location(_MODULE, init)
        if spec is None or spec.loader is None:
            problems.append(f"{init}: not importable")
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = (
            module  # its @dataclass resolves its own module through sys.modules
        )
        spec.loader.exec_module(module)
        return cast(_HatchLib, module)
    raise HatchUnavailable("; ".join(problems))


def failing_summary(checks: Sequence[Check], budget: int | None = None) -> str:
    """One `- name: detail` line per FAIL (details capped), the text the owner reads on Telegram.

    `budget` bounds the whole text: the card helper truncates a context value past `_CARD_VALUE_CHARS`,
    which would silently drop the last FAILs, so the details shrink to fit and every name stays."""
    failed = [c for c in checks if c.status == FAIL]
    cap = _DETAIL_CHARS
    if budget is not None and failed:
        overhead = sum(len(c.name) + 5 for c in failed)
        cap = max(20, min(_DETAIL_CHARS, (budget - overhead) // len(failed)))
    lines = []
    for c in failed:
        detail = " ".join(c.detail.split())
        if len(detail) > cap:
            detail = detail[: cap - 1] + "…"
        lines.append(f"- {c.name}: {detail}")
    return "\n".join(lines)


def _refusal(
    failing: str, why: str, command: str, extra: Sequence[str] = ()
) -> PrinterRefused:
    return PrinterRefused(
        f"--force does not override preflight FAIL by itself: {why}",
        command="printer",
        remediation=[
            "Fix the cause of each FAIL (a real defect: do not print; a false positive: fix the gate — "
            "open an issue and a PR for it).",
            f"Only if forcing is truly needed, ask the owner for a one-time Telegram approval: "
            f'{ENV_VAR}="<why this FAIL is acceptable>" {command}',
            "Every forced case is analysed afterwards and its root cause fixed.",
            *extra,
            "Failing checks:\n" + failing,
        ],
    )


def _send_failed(verdict: _Verdict) -> bool:
    """The daemon declined to post the card (`ask-forward not-posted … reason=send-failed`): its
    Telegram send failed (a DNS or network blip, 2026-11-04 `getaddrinfo EAI_AGAIN api.telegram.org`).
    No card exists then, so asking again cannot double-approve."""
    return not verdict.approved and _SEND_FAILED in verdict.reason


def _ask_with_retry(
    hatch: _HatchLib, context: dict[str, object], command: str
) -> _Verdict:
    """`request_hatch_approval`, asked again after each `RETRY_DELAYS_S` pause while the card could not
    be posted. Any other verdict (approved, denied, timed out, not requested) is final at once."""
    verdict = hatch.request_hatch_approval(
        HOOK_ID, context, cwd=os.getcwd(), command=command, critical=True
    )
    for delay in RETRY_DELAYS_S:
        if not _send_failed(verdict):
            break
        print(
            f"hatch: the approval card was not posted to Telegram ({verdict.reason}); asking again in {delay:g} s",
            file=sys.stderr,
        )
        time.sleep(delay)
        verdict = hatch.request_hatch_approval(
            HOOK_ID, context, cwd=os.getcwd(), command=command, critical=True
        )
    return verdict


def _screen_hint(remote_name: str | None) -> list[str]:
    """How to start without Telegram: from the printer's own screen."""
    what = f"`{remote_name}`" if remote_name else "the file"
    return [
        f"Start from the printer screen instead: {what} is already stored on the printer (Files, then Print). "
        "The screen does NOT run these checks: only do it when the FAIL lines below are understood.",
        "Why Telegram failed: tg-ctl's daemon could not send the card (plain `tg` can still work); see "
        "`~/.config/tg-cli/tg-ctl.*.log`, lines `sendMessage failed` / `ask-forward dropped: reason=send-failed`.",
    ]


def require_force_approval(
    *,
    printer: str,
    file: str,
    sha256: str,
    checks: Sequence[Check],
    remote_name: str | None = None,
) -> str:
    """Return the approval reason when the owner approved the forced start, else raise PrinterRefused.

    Always consults the helper, even with the env var unset: it then looks for a late-approval
    grant minted by an earlier tap, and otherwise reports "not requested"."""
    failing = failing_summary(checks)
    command = (
        f"3d printer start {shlex.quote(printer)} {shlex.quote(file)} --yes --force"
    )
    try:
        hatch = load_hatch()
    except HatchUnavailable as exc:
        raise _refusal(
            failing, f"the hatch helper is unavailable ({exc}); refusing", command
        ) from exc
    context: dict[str, object] = {
        "hook": HOOK_ID,
        "command": command,
        "printer": printer,
        "file": file,
        "sha256": sha256,
        "failing_checks": failing_summary(checks, _CARD_VALUE_CHARS - 50),
    }
    verdict = _ask_with_retry(hatch, context, command)
    if verdict.approved:
        return verdict.reason
    if _send_failed(verdict):
        raise _refusal(
            failing,
            f"the approval request could not be posted to Telegram after {1 + len(RETRY_DELAYS_S)} attempts "
            f"({verdict.reason})",
            command,
            extra=_screen_hint(remote_name),
        )
    if verdict.should_stop:
        raise _refusal(
            failing,
            f"the Telegram hatch request was not approved ({verdict.reason})",
            command,
        )
    raise _refusal(
        failing,
        f"no approval requested (set {ENV_VAR} to a written justification to ask the owner)",
        command,
    )
