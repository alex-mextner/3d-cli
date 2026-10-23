"""auto_pause.py — the watcher pauses a print itself when nobody could be asked (#529, tg#24096).

WHAT: the last resort of alert delivery. When the subscribed omp sessions and the temporary
agent all failed to take a critical alert (or did not acknowledge it in time), the watcher
may pause the CURRENT print through the adapter's normal pause path, then tell the user.
`resolve_policy` decides at startup whether a printer may ever be paused this way,
`qualifying_event` picks the few high-confidence critical findings that justify it, and
`AutoPauser.pause` does the single bound, claimed, verified pause.

WHY: a spaghetti print left running for hours wastes filament and can damage the hotend, but
a pause is always safe to undo and a wrong cancel is not. So the only motion command here is
the native PAUSE, the evidence bar equals `printer guard`'s (verified, vetoed-by-CLIP-safe
spaghetti; a confidently detected detached part), and the person decides about resume/cancel.

ACCESSED VIA: the staged delivery sinks of `3d printer watch` / `3d printer vision`
(printer_ops/watch_sinks.py) call `AutoPauser.pause` after the agent stage failed; the opt-in
`watch.pause_without_agent` key is read through `resolve_policy`.

INVARIANTS:
- Only `adapter.pause()` is ever sent; never cancel, resume, start or G-code. Never a retry.
- Once per job, across processes AND across both pausers: the exclusive claim file
  `<state>/guard/NAME-JOBID.json` is the one `printer guard` uses; it stays after pause() is
  entered, whatever the outcome.
- The job is re-bound from the printer's own state and history at the moment of the pause
  (printing, newest in_progress record == current file, event not older than the job start).
- A printer owned by someone else, or one we cannot fully control, is never paused: an explicit
  `critical-only` for it fails loudly at startup instead of being honoured.
- `pause` never raises for adapter/filesystem trouble; it returns a PauseResult saying what
  did (not) happen. A failed journal write never changes the pause outcome.
- stdlib only at import time.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument
from printer_ops.base import PAUSED, PRINTING, Adapter
from printer_ops.guard import _NAME, POLICY_VERSION, _sync_directory, _write_result
from printer_ops.vision import VETO_RATIO
from printer_ops.watch import CRITICAL, WARNING, Event

if TYPE_CHECKING:
    from printer_ops.devices import Device
    from printops.fleet import Fleet

POLICIES = ("critical-only", "off")
POLL_S = 0.5
SOURCE = "watch_auto_pause"
ACTOR = "3d-printer-watch (auto pause without agent)"
JOURNAL_COMMAND = "3d printer watch (pause_without_agent)"
VERIFIED_BOX_MIN = 0.5


def _number(value: object) -> float | None:
    """A finite real number (bool is not one), else None."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _verified_spaghetti(e: Event) -> bool:
    """The evidence bar of `PauseGuard.observe`: the verifier ran, kept at least VETO_RATIO of
    the raw score, and one region is verified as a failure."""
    p, q = _number(e.data.get("p")), _number(e.data.get("q"))
    if p is None or q is None or q < VETO_RATIO * p:
        return False
    boxes = e.data.get("boxes")
    if not isinstance(boxes, list):
        return False
    for box in boxes:
        verified = _number(box.get("verified")) if isinstance(box, dict) else None
        if verified is not None and verified >= VERIFIED_BOX_MIN:
            return True
    return False


def _confident_detach(e: Event) -> bool:
    return e.data.get("confidence") != "low"


# The ONE table of what may pause a print unattended: event kind -> extra evidence check (all of
# them also need severity critical). A new kind (the D5 "dangerous" preflight kinds) is one line.
QUALIFYING: Mapping[str, Callable[[Event], bool]] = {
    "vision_spaghetti": _verified_spaghetti,
    "vision_part_detached": _confident_detach,
}


def qualifying_event(
    events: list[Event], *, now: float, max_age_s: float = 600.0
) -> Event | None:
    """The newest event that justifies a pause, or None (module doc, INVARIANTS)."""
    best: Event | None = None
    for e in events:
        check = QUALIFYING.get(e.kind)
        if check is None or e.severity != CRITICAL or not check(e):
            continue
        if not math.isfinite(e.ts) or now - e.ts > max_age_s:
            continue
        if best is None or e.ts > best.ts:
            best = e
    return best


def resolve_policy(
    device: Device, configured: object | None, *, fleet: Fleet | None = None
) -> str:
    """`critical-only` or `off` for `<printer>.watch.pause_without_agent` (`configured`, None =
    unset). Unset enables it only for our own, fully controlled Moonraker printer with a camera;
    an explicit `critical-only` for anything else raises InvalidArgument (exit 2)."""
    from printer_ops.camera import camera_spec

    if configured is not None and configured not in POLICIES:
        raise InvalidArgument(
            f"{device.name}.watch.pause_without_agent",
            str(configured),
            POLICIES,
            command="printer watch",
        )
    if configured == "off":
        return "off"
    if fleet is None:
        from printops.fleet import load_fleet

        fleet = load_fleet()
    entry = fleet.printers.get(device.name) if fleet is not None else None
    own_full = entry is not None and entry.owner == "own" and entry.access == "full"
    controllable = device.access == "full" and device.adapter == "moonraker"
    if configured is None:
        return (
            "critical-only"
            if own_full and controllable and camera_spec(device).present
            else "off"
        )
    if not controllable or (entry is not None and not own_full):
        why = (
            f"owner {entry.owner}, access {entry.access}"
            if entry is not None and not own_full
            else f"adapter {device.adapter}, access {device.access}"
        )
        raise InvalidArgument(
            f"{device.name}.watch.pause_without_agent",
            "critical-only",
            ["off"],
            command="printer watch",
            extra=(
                f"{device.name} cannot be paused unattended ({why}): only our own fully controlled "
                "Moonraker printers can. Set pause_without_agent: off or remove the key."
            ),
        )
    return "critical-only"


@dataclass(frozen=True)
class PauseResult:
    printer: str
    outcome: str  # paused | pause_unverified | failed | skipped
    detail: str  # one plain sentence: why / what happened (no secrets)
    job_id: str | None
    filename: str | None
    event: Event  # the qualifying event
    frame: str | None  # first frame path of the event that exists on disk
    webui: str | None = None  # http://HOST of the printer (Fluidd/Mainsail), for the resume hint

    def to_event(self) -> Event:
        severity = (
            CRITICAL if self.outcome in ("failed", "pause_unverified") else WARNING
        )
        what = {
            "paused": "paused the print without an agent",
            "pause_unverified": "asked the printer to pause but it did not confirm",
            "failed": "could NOT pause the print",
            "skipped": "did not pause the print",
        }[self.outcome]
        return Event(
            self.printer,
            "auto_pause",
            severity,
            f"watcher {what}: {self.detail}",
            {
                "outcome": self.outcome,
                "detail": self.detail,
                "job_id": self.job_id,
                "filename": self.filename,
                "trigger_kind": self.event.kind,
                "trigger_key": self.event.key
                or f"{self.event.printer}:{self.event.kind}",
                "frame": self.frame,
            },
            key=f"{self.printer}:auto_pause:{self.job_id or 'none'}",
            ts=self.event.ts,
        )

    def message(self) -> str:
        lines = [f"{self.printer}: {self.event.text}"]
        if self.outcome == "paused":
            lines.append(
                "Nobody took the alert, so the watcher PAUSED the print "
                f"({self.filename or 'current job'})."
            )
            where = f" at {self.webui}" if self.webui else ""
            lines.append(
                f"To resume: on the printer's touchscreen or in Fluidd/Mainsail{where} press Resume, "
                "or Cancel there. The watcher never resumes or cancels by itself."
            )
        elif self.outcome == "skipped":
            lines.append(f"The watcher did not pause the print: {self.detail}")
        else:
            lines.append(f"The watcher could NOT pause the print: {self.detail}")
            lines.append("Check the printer now.")
        if self.frame:
            lines.append(f"Frame: {self.frame}")
        return "\n".join(lines)


def default_claim_dir() -> Path:
    """Where `printer guard` keeps NAME-JOBID.json claims (vision_detect.run_guard)."""
    from cli.paths import state_dir

    return state_dir() / "guard"


def _first_frame(event: Event) -> str | None:
    from printer_ops.subscriptions import frames_of

    return next((f for f in frames_of(event) if Path(f).is_file()), None)


def _utc(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class AutoPauser:
    def __init__(
        self,
        printer: str,
        *,
        adapter: Callable[[], Adapter] | None = None,
        claim_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        verify_s: float = 8.0,
        max_event_age_s: float = 600.0,
    ) -> None:
        self.printer = printer
        self._make_adapter = adapter or self._default_adapter
        self.claim_dir = claim_dir
        self.clock, self.sleep = clock, sleep
        self.verify_s, self.max_event_age_s = verify_s, max_event_age_s

    def _default_adapter(self) -> Adapter:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # The fleet registry names the real printers: a test that reaches this default would pause one.
            raise RuntimeError("refusing to contact a printer from a test: inject adapter=")
        from printer_ops import make_adapter
        from printer_ops.devices import get_device

        return make_adapter(get_device(self.printer))

    def pause(self, events: list[Event]) -> PauseResult | None:
        event = qualifying_event(
            events, now=self.clock(), max_age_s=self.max_event_age_s
        )
        if event is None:
            return None
        frame = _first_frame(event)

        def result(
            outcome: str,
            detail: str,
            job_id: str | None = None,
            filename: str | None = None,
            webui: str | None = None,
        ) -> PauseResult:
            return PauseResult(
                self.printer, outcome, detail, job_id, filename, event, frame, webui
            )

        try:
            adapter = self._make_adapter()
        except (
            Exception
        ) as exc:  # adapter boundary: any failure is a report, not a crash
            return result("failed", f"cannot reach the printer: {str(exc)[:200]}")
        # Fluidd/Mainsail answer on the host's port 80; adapter.port is the Moonraker API (7125).
        webui = f"http://{adapter.host}"
        try:
            status = adapter.status()
        except Exception as exc:
            return result(
                "failed",
                f"cannot read the printer state: {str(exc)[:200]}",
                webui=webui,
            )
        if status.state != PRINTING:
            return result(
                "skipped", f"the printer is {status.state}, not printing", webui=webui
            )
        try:
            jobs = adapter.history(1)
        except Exception as exc:
            return result(
                "failed", f"cannot read the job history: {str(exc)[:200]}", webui=webui
            )
        record = jobs[0] if jobs else None
        if (
            record is None
            or record.status != "in_progress"
            or not record.job_id
            or not status.filename
            or record.filename != status.filename
            or record.start_time is None
            or not _NAME.fullmatch(record.job_id)
            or not _NAME.fullmatch(self.printer)
        ):
            return result(
                "skipped",
                "cannot bind the current job to the printer's history, so pausing "
                "once per job cannot be guaranteed",
                webui=webui,
            )
        job_id, filename, start = record.job_id, record.filename, record.start_time

        def bound(outcome: str, detail: str) -> PauseResult:
            return result(outcome, detail, job_id, filename, webui)

        if event.ts < start:
            return bound("skipped", "the alert is older than the current job's start")
        claim_dir = self.claim_dir or default_claim_dir()
        claim_path = claim_dir / f"{self.printer}-{job_id}.json"
        payload: dict[str, Any] = {
            "printer": self.printer,
            "job_id": job_id,
            "filename": filename,
            "start_time": start,
            "ts": self.clock(),
            "outcome": "pause_requested",
            "terminal": False,
            "action": "pause",
            "authorized": True,
            "dry_run": False,
            "pid": os.getpid(),
            "policy_version": POLICY_VERSION,
            "source": SOURCE,
            "trigger_kind": event.kind,
            "frame": frame,
        }
        try:
            claim_dir.mkdir(parents=True, exist_ok=True)
            stream = claim_path.open("x")
        except FileExistsError:
            return bound(
                "skipped",
                "a pause was already attempted for this job (by the watcher or "
                f"`printer guard`); see {claim_path}",
            )
        except OSError as exc:
            return bound(
                "failed",
                f"cannot create the pause claim, nothing was sent: {exc.strerror or exc}",
            )
        with stream:
            claim = os.fstat(stream.fileno())
            json.dump(payload, stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(claim_dir)

        acknowledged, error = False, None
        try:
            adapter.pause()
            acknowledged = True
        except (
            Exception
        ) as exc:  # refusal, comm error or bug: never retried, the claim stays
            error = str(exc)[:200]
        deadline = self.clock() + self.verify_s
        paused = False
        while True:
            try:
                paused = adapter.status().state == PAUSED
            except Exception as exc:
                error = error or str(exc)[:200]
            if paused or self.clock() >= deadline:
                break
            self.sleep(POLL_S)
        if paused:
            outcome = "paused"
            detail = (
                f"the printer reports paused (the pause request failed: {error})"
                if error and not acknowledged
                else "the printer reports paused"
            )
        elif acknowledged:
            outcome, detail = (
                "pause_unverified",
                f"the pause request was accepted but the printer was not paused after {self.verify_s:g} s",
            )
        else:
            outcome, detail = "failed", f"the pause request failed: {error}"
        final = {
            **payload,
            "ts": self.clock(),
            "outcome": outcome,
            "terminal": True,
            "pause_acknowledged": acknowledged,
            "error": error,
        }
        try:
            current = claim_path.lstat()
            if (current.st_dev, current.st_ino) == (claim.st_dev, claim.st_ino):
                _write_result(claim_path, final)
        except OSError as exc:
            detail += f"; the claim file could not be updated: {exc.strerror or exc}"
        try:
            self._journal(outcome, job_id, filename, frame, claim_path)
        except Exception as exc:  # the pause stands; the journal is bookkeeping
            print(
                f"auto-pause: {self.printer}: settings journal write failed: {exc}",
                file=sys.stderr,
            )
            detail += (
                f"; the settings journal line could not be written: {str(exc)[:120]}"
            )
        return bound(outcome, detail)

    def _journal(
        self, outcome: str, job_id: str, filename: str, frame: str | None, claim: Path
    ) -> None:
        from printops import history_buffer
        from printops.printer_task_buffer import uuid7

        stamp = _utc(self.clock())
        paused = outcome == "paused"
        history_buffer.append(
            self.printer,
            {
                "uid": uuid7(),
                "schema": history_buffer.SCHEMA,
                "printer": self.printer,
                "at": stamp,
                "recorded_at": stamp,
                "actor": ACTOR,
                "kind": "other",
                "key": "print.pause",
                "old": {"state": "printing"},
                "new": {"state": "paused" if paused else "printing"},
                "status": "observed" if paused else "measured_not_applied",
                "source": SOURCE,
                "command": JOURNAL_COMMAND,
                "job_id": job_id,
                "filename": filename,
                "evidence": [x for x in (frame, str(claim), job_id) if x],
            },
        )
