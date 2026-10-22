"""Explicit, one-job automatic PAUSE. Vision/watch remain read-only.

`printer guard NAME --job-id ID --yes` authorizes one native PAUSE for that live Moonraker
job, after two distinct fresh frames reach the existing verified critical failure level.
No notification delivery, agent judgement, resume, cancel, G-code or setting change is in
this path. A durable pre-send claim prevents retries after a lost response or a restart.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errors import UsageError
from printer_ops.base import PAUSED, PRINTING, Adapter, JobRecord, PrinterCommError, PrinterRefused, PrinterStatus
from printer_ops.vision import CRITICAL, VETO_RATIO, FailureTracker, Frame, FrameScore, MonitorLimits

MAX_FRAME_AGE_S = 30.0
DEFAULT_MAX_ACQUISITION_GAP_S = 30.0
ACQUISITION_MARGIN_S = 2.0
RECENT_FRAME_LIMIT = 64
VERIFY_PAUSE_S = 8.0
CONFIRM_FRAMES = 2
POLICY_VERSION = 1
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")


@dataclass(frozen=True)
class BoundJob:
    job_id: str
    filename: str
    start_time: float


def _snapshot(adapter: Adapter) -> tuple[PrinterStatus, JobRecord]:
    status = adapter.status()
    jobs = adapter.history(1)
    if not jobs:
        raise PrinterRefused("current native job history is empty", command="printer guard")
    return status, jobs[0]


def _duration(status: PrinterStatus) -> float | None:
    value = (status.raw.get("print_stats") or {}).get("total_duration")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _write_result(path: Path, data: dict[str, Any]) -> None:
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PauseGuard:
    def __init__(self, adapter: Adapter, job: BoundJob, *, state_dir: Path, evidence_dir: Path,
                 clock: Callable[[], float], sleep: Callable[[float], None], dry_run: bool,
                 interval: float = 10.0) -> None:
        self.adapter, self.job = adapter, job
        self.clock, self.sleep, self.dry_run = clock, sleep, dry_run
        self.evidence_dir = evidence_dir
        self.attempt_path = state_dir / f"{adapter.name}-{job.job_id}.json"
        self.max_acquisition_gap_s = max(DEFAULT_MAX_ACQUISITION_GAP_S, interval + ACQUISITION_MARGIN_S)
        self.tracker = FailureTracker()
        self.confirmations = 0
        self.last_digest: str | None = None
        self.last_frame_ts: float | None = None
        # Replay memory survives blindness; only new unique images evict old hashes.
        self.recent_digests: deque[str] = deque(maxlen=RECENT_FRAME_LIMIT)
        self.latest_source_modified: float | None = None
        self.duration: float | None = None
        self.result: dict[str, Any] | None = None

    @classmethod
    def arm(cls, adapter: Adapter, job_id: str, *, state_dir: Path, evidence_dir: Path,
            clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
            dry_run: bool = False, interval: float = 10.0) -> PauseGuard:
        if not _NAME.fullmatch(job_id) or not _NAME.fullmatch(adapter.name):
            raise UsageError("printer and --job-id need safe nonempty identifiers", command="printer guard")
        status, record = _snapshot(adapter)
        if (status.state != PRINTING or record.status != "in_progress" or record.job_id != job_id
                or not status.filename or record.filename != status.filename or record.start_time is None):
            raise PrinterRefused(f"{adapter.name}: --job-id {job_id} is not the current printing job",
                                 command="printer guard")
        guard = cls(adapter, BoundJob(job_id, record.filename, record.start_time), state_dir=state_dir,
                    evidence_dir=evidence_dir, clock=clock, sleep=sleep, dry_run=dry_run, interval=interval)
        if guard.attempt_path.exists():
            raise PrinterRefused(f"{adapter.name} job {job_id}: pause already attempted; inspect {guard.attempt_path}",
                                 command="printer guard")
        guard.duration = _duration(status)
        return guard

    def _report(self, outcome: str, *, terminal: bool = False, **data: Any) -> dict[str, Any]:
        result = {"printer": self.adapter.name, "job_id": self.job.job_id, "filename": self.job.filename,
                  "start_time": self.job.start_time, "ts": self.clock(), "outcome": outcome,
                  "terminal": terminal, "confirmations": self.confirmations, "action": "pause",
                  "authorized": not self.dry_run, "dry_run": self.dry_run, "pid": os.getpid(),
                  "policy_version": POLICY_VERSION,
                  "camera_ready": outcome in ("observing", "would_pause", "paused", "pause_unverified")
                                  and "frame_ts" in data, **data}
        if terminal:
            self.result = result
        return result

    def _blind(self, reason: str) -> dict[str, Any]:
        self.confirmations = 0
        # Interrupted warmups must not train a high lifetime baseline that hides later failure.
        self.tracker = FailureTracker()
        return self._report("blind", reason=reason)

    def _current(self, *, allow_paused: bool = False) -> PrinterStatus:
        status, record = _snapshot(self.adapter)
        if (status.state not in ((PRINTING, PAUSED) if allow_paused else (PRINTING,))
                or status.filename != self.job.filename or record.filename != self.job.filename
                or record.job_id != self.job.job_id or record.start_time != self.job.start_time
                or record.status != "in_progress"):
            raise PrinterRefused("bound job ended, paused or changed; guard is disarmed", command="printer guard")
        duration = _duration(status)
        if duration is not None and self.duration is not None and duration + 1.0 < self.duration:
            raise PrinterRefused("print duration reset; job identity is uncertain", command="printer guard")
        self.duration = duration
        return status

    def _freshness_error(self, frame: Frame) -> str | None:
        """Host, Moonraker and camera wall clocks must be synchronized; never guess an offset."""
        now = self.clock()
        if not math.isfinite(now):
            return "guard host clock is not finite"
        if not math.isfinite(frame.ts):
            return "camera acquisition time is not finite"
        if frame.ts > now:
            return "camera acquisition time is in the future; check the guard host clock"
        if now - frame.ts > MAX_FRAME_AGE_S:
            return "camera acquisition is older than 30 seconds; stale frame or host clock jump"
        if frame.ts < self.job.start_time:
            return "camera acquisition predates the native job start; check host/printer clock synchronization"
        # Source age matters: grabbing a cached monitor.jpg now does not make its image new.
        if frame.modified is not None:
            if not math.isfinite(frame.modified):
                return "camera Last-Modified is not finite"
            if frame.modified > now + 5:
                return "camera Last-Modified is more than 5 seconds ahead; check host/printer clock synchronization"
            if now - frame.modified > MAX_FRAME_AGE_S:
                return "camera Last-Modified is older than 30 seconds; stale camera or unsynchronized clocks"
            if frame.modified < self.job.start_time:
                return "camera Last-Modified predates the native job start; old frame or unsynchronized clocks"
            if self.latest_source_modified is not None and frame.modified < self.latest_source_modified:
                return "camera Last-Modified moved backward; reordered source evidence or clock change"
        return None

    def _owns_claim(self, claim: os.stat_result) -> bool:
        try:
            current = self.attempt_path.lstat()
        except FileNotFoundError:
            return False
        return (current.st_dev, current.st_ino) == (claim.st_dev, claim.st_ino)

    def _release_unsent_claim(self, claim: os.stat_result, reason: str, *, terminal: bool = False) -> dict[str, Any]:
        """Only before entering pause(), with our exclusive claim descriptor still open."""
        if not self._owns_claim(claim):
            return self._report("disarmed", terminal=True, reason="pause claim ownership changed; no PAUSE sent")
        self.attempt_path.unlink()
        _sync_directory(self.attempt_path.parent)
        reason = f"{reason}; unsent pause claim released"
        if terminal:
            return self._report("disarmed", terminal=True, reason=reason)
        return self._blind(reason)

    def observe(self, frame: Frame | None, score: FrameScore | None) -> dict[str, Any]:
        if self.result is not None:
            return self.result
        try:
            self._current()
        except PrinterRefused as exc:
            return self._report("disarmed", terminal=True, reason=str(exc))
        except PrinterCommError as exc:
            return self._blind(str(exc))
        if frame is None or score is None:
            return self._blind("no fresh scored camera frame")
        if reason := self._freshness_error(frame):
            return self._blind(reason)
        valid = (score.q is not None and math.isfinite(score.q) and math.isfinite(score.p)
                 and math.isfinite(score.luma) and 0 <= score.q <= score.p
                 and score.luma >= MonitorLimits().dark_luma)
        if not valid:
            return self._blind("no usable verified score")
        digest = hashlib.sha256(frame.jpeg).hexdigest()
        repeated = digest in self.recent_digests
        if self.last_frame_ts is not None and frame.ts <= self.last_frame_ts:
            return self._blind("reordered camera evidence")
        # A cached JPEG may have a rewritten header; only a distinct usable image
        # (including a new post-gap anchor) can advance source ordering.
        if not repeated and frame.modified is not None:
            self.latest_source_modified = frame.modified
        if self.last_frame_ts is not None and frame.ts - self.last_frame_ts > self.max_acquisition_gap_s:
            # A distinct returning image establishes a new acquisition anchor; the
            # first image after the gap still cannot contribute failure evidence.
            if not repeated:
                self.recent_digests.append(digest)
                self.last_digest, self.last_frame_ts = digest, frame.ts
            return self._blind("discontinuous camera evidence")
        if repeated:
            # Polling can outrun the camera. A fresh duplicate is neutral, never a new
            # confirmation or a refresh of the last distinct image's continuity bound.
            return self._report("waiting", reason="duplicate camera frame; waiting for a distinct image",
                                frame_ts=frame.ts, frame_modified=frame.modified, frame_sha256=digest)
        self.recent_digests.append(digest)
        self.last_digest, self.last_frame_ts = digest, frame.ts
        # Healthy verified frames (q=0, no boxes) must decay the previous failure's trend.
        level = self.tracker.update(score.signal)
        positive = (score.q is not None and score.q >= VETO_RATIO * score.p
                    and any(math.isfinite(p) and p >= 0.5 for p in score.probs))
        self.confirmations = self.confirmations + 1 if level == CRITICAL and positive else 0
        data: dict[str, Any] = {"p": score.p, "q": score.q, "trend": self.tracker.adjusted, "level": level,
                "frame_ts": frame.ts, "frame_modified": frame.modified, "frame_sha256": digest}
        if self.confirmations < CONFIRM_FRAMES:
            return self._report("observing", **data)
        # Evidence and a single-use claim precede the write. Identity is re-read after them.
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = self.evidence_dir / f"{self.adapter.name}-{self.job.job_id}-{digest[:16]}.jpg"
        evidence.write_bytes(frame.jpeg)
        data["frame"] = str(evidence)
        if self.dry_run:
            return self._report("would_pause", terminal=True, **data)
        self.attempt_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = self.attempt_path.open("x")
        except FileExistsError:
            return self._report("disarmed", terminal=True, reason="pause already attempted by another guard")
        # Keep the inode alive until disposition, so a replacement cannot reuse its identity.
        with stream:
            claim = os.fstat(stream.fileno())
            json.dump(self._report("pause_requested", **data), stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            _sync_directory(self.attempt_path.parent)
            try:
                self._current()
            except PrinterCommError as exc:
                return self._release_unsent_claim(claim, f"pre-pause job check failed: {exc}")
            except PrinterRefused as exc:
                return self._release_unsent_claim(claim, str(exc), terminal=True)
            if not self._owns_claim(claim):
                return self._report("disarmed", terminal=True, reason="pause claim ownership changed; no PAUSE sent")
            if reason := self._freshness_error(frame):
                return self._release_unsent_claim(claim, f"pre-pause frame rejected: {reason}")
            acknowledged, error = False, None
            try:
                self.adapter.pause()
                acknowledged = True
            except (PrinterRefused, PrinterCommError) as exc:
                error = str(exc)
            # Once pause() is entered the claim stays, even after a refusal or lost response.
            deadline = self.clock() + VERIFY_PAUSE_S
            paused = False
            while True:
                try:
                    paused = self._current(allow_paused=True).state == PAUSED
                except (PrinterRefused, PrinterCommError) as exc:
                    error = str(exc)
                if paused or self.clock() >= deadline:
                    break
                self.sleep(0.5)
            result = self._report("paused" if paused else "pause_unverified", terminal=True,
                                  pause_acknowledged=acknowledged, error=error, **data)
            if self._owns_claim(claim):
                _write_result(self.attempt_path, result)
            else:
                result["claim_error"] = "pause claim ownership changed; existing file preserved"
            return result


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.epilog = ("Examples: 3d printer guard u1 --job-id 00006D --yes; "
                     "3d printer guard u1 --job-id 00006D --dry-run --interval 10 "
                     "--evidence ~/.cache/3d-cli/guard-check")
    parser.add_argument("printer")
    parser.add_argument("--job-id", required=True, help="exact current Moonraker native job ID; never a filename")
    parser.add_argument("--yes", action="store_true", help="authorize one automatic PAUSE of this job")
    parser.add_argument("--dry-run", action="store_true", help="record would-pause evidence; never send PAUSE")
    parser.add_argument("--interval", type=float, default=10.0, help="capture interval in seconds (2..30; default 10)")
    parser.add_argument("--evidence", help="local evidence directory")


def validate_args(args: argparse.Namespace) -> None:
    if not args.yes and not args.dry_run:
        raise UsageError("automatic physical pause needs --yes", command="printer guard")
    if not _NAME.fullmatch(args.job_id):
        raise UsageError("--job-id must be a native job identifier", command="printer guard")
    if not math.isfinite(args.interval) or not 2 <= args.interval <= 30:
        raise UsageError("--interval must be 2..30 seconds", command="printer guard")


def guard_device(name: str) -> Any:
    from printer_ops.devices import get_device

    device = get_device(name)
    if device.access != "full" or device.adapter != "moonraker":
        raise PrinterRefused(f"{name}: guard requires full Moonraker access; got {device.access}/{device.adapter}",
                             command="printer guard")
    return device


def run(argv: list[str], *, vision_deps: str) -> int:
    parser = argparse.ArgumentParser(prog="3d printer guard", description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    validate_args(args)
    guard_device(args.printer)  # refuse before the model runtime or any printer requests
    from cli.pyrun import exec_tool

    return exec_tool(vision_deps, "vision_detect.py", ["guard", *argv])
