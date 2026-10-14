"""vision.py — camera frames, the printer's own detector, and the decision logic of `3d printer vision`.

WHAT: the stdlib half of camera failure detection (issue
https://github.com/alex-mextner/3d-cli/issues/150); the model and pixel work live in
lib/vision_detect.py (numpy/OpenCV/onnxruntime), which drives `VisionMonitor` below.
  * `MoonrakerCamera` grabs JPEG frames. On the Snapmaker U1 stock firmware there is no MJPEG
    streamer (nginx /webcam/ answers 502): the camera service `unisrv` writes
    `camera/monitor.jpg` about every 4 s, but only while a client keeps it awake with the
    JSON-RPC call `camera.start_monitor` (Snapmaker Orca does the same). `keepalive()` sends
    exactly that call; it wakes the camera and changes nothing else.
  * `KlippyLogTap` reads the U1's own detector. The U1 runs a YOLO "noodle" model on its NPU and
    Klipper's `defect_detection` logs every verdict to klippy.log. Moonraker does not expose
    the detector (`camera.detect_capture` -> -32601 Method not found), so the verdicts are
    read by tailing klippy.log with HTTP Range requests.
  * `FailureTracker` turns per-frame spaghetti scores into warning/critical levels. It follows
    the published Obico scheme (EWM of the score minus a long rolling baseline, thresholds
    0.38/0.78, escalation x1.75; obico-server backend/lib/prediction.py), re-implemented here.
  * `VisionMonitor` runs one step: frame -> score -> levels/camera checks/NPU verdicts ->
    edge-triggered `printer_ops.watch.Event`s (kinds vision_spaghetti, vision_camera_stale,
    vision_camera_dark, printer_noodle_verdict).

INVARIANTS: towards the printer only GETs plus `camera.start_monitor`. Never pause, cancel or
change detector settings from here; the user or the agent decides on the events.
"""

from __future__ import annotations

import ast
import email.utils
import http.client
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from printer_ops.base import PAUSED, PRINTING, PrinterCommError

U1_MONITOR = "/server/files/camera/monitor.jpg"
KEEPALIVE_S = (
    60.0  # camera.start_monitor renewal; the U1 monitor lapses after a few minutes
)
GRAB_RETRIES = (
    3  # monitor.jpg is rewritten in place, so a read can hit a half-written file
)
TAIL_BYTES = (
    64 * 1024
)  # first klippy.log read: the last 64 KiB (a few minutes of verdicts)


@dataclass
class Frame:
    ts: float  # wall clock when grabbed (s since epoch)
    jpeg: bytes
    modified: float | None  # Last-Modified of the snapshot, when the server sends it


def is_complete_jpeg(data: bytes) -> bool:
    """SOI at the start and EOI at the end: the cheap test for a half-written monitor.jpg."""
    return (
        len(data) > 4
        and data[:2] == b"\xff\xd8"
        and data.rstrip(b"\x00")[-2:] == b"\xff\xd9"
    )


def _get(
    url: str, timeout: float, headers: dict[str, str] | None = None
) -> tuple[int, bytes, dict[str, str]]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "", parts.port or 80, timeout=timeout
    )
    try:
        conn.request(
            "GET",
            (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
            headers=headers or {},
        )
        r = conn.getresponse()
        return r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
    finally:
        conn.close()


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


class MoonrakerCamera:
    """Snapshots from a Moonraker printer: the U1 monitor.jpg, or any snapshot URL/path."""

    def __init__(
        self,
        host: str,
        port: int = 7125,
        *,
        snapshot: str = U1_MONITOR,
        keepalive: bool | None = None,
        timeout: float = 10.0,
        retry_delay: float = 1.0,
    ) -> None:
        self.host, self.port, self.timeout, self.retry_delay = (
            host,
            port,
            timeout,
            retry_delay,
        )
        self.url = snapshot if "://" in snapshot else f"http://{host}:{port}{snapshot}"
        # Only the Snapmaker monitor file needs waking; a real streamer serves frames on its own.
        self.keepalive_enabled = (
            snapshot == U1_MONITOR if keepalive is None else keepalive
        )
        self._last_keepalive = float("-inf")

    def keepalive(self, now: float | None = None) -> bool:
        """Send `camera.start_monitor` when the last one is older than KEEPALIVE_S.

        Fire-and-forget: Moonraker's repeater forwards it to unisrv and no reply comes back.
        Returns True when a call was sent."""
        now = time.time() if now is None else now
        if not self.keepalive_enabled or now - self._last_keepalive < KEEPALIVE_S:
            return False
        from printer_ops.ws import WebSocket, WebSocketError

        msg = {
            "jsonrpc": "2.0",
            "method": "camera.start_monitor",
            "params": {"domain": "lan", "interval": 0},
            "id": int(now * 1000),
        }
        try:
            with WebSocket.connect(
                self.host, self.port, "/websocket", timeout=self.timeout
            ) as ws:
                ws.send_text(json.dumps(msg))
        except (OSError, WebSocketError) as exc:
            raise PrinterCommError(
                f"camera keepalive to {self.host}:{self.port} failed: {exc}",
                command="printer vision",
            ) from exc
        self._last_keepalive = now
        return True

    def grab(self) -> Frame:
        """One complete JPEG. Retries a 5xx or a truncated body: unisrv rewrites the file in place
        and Moonraker then answers 500 (FileNotFoundError) or a short body."""
        last = ""
        for attempt in range(GRAB_RETRIES):
            if attempt:
                time.sleep(self.retry_delay)
            ts = time.time()
            sep = "&" if urlsplit(self.url).query else "?"
            try:
                status, data, headers = _get(
                    f"{self.url}{sep}t={int(ts * 1000)}", self.timeout
                )
            except OSError as exc:
                last = str(exc)
                continue
            if status != 200:
                last = f"HTTP {status}"
                continue
            if not is_complete_jpeg(data):
                last = f"incomplete JPEG ({len(data)} bytes)"
                continue
            return Frame(ts, data, _http_date(headers.get("last-modified")))
        raise PrinterCommError(
            f"no camera frame from {self.url} after {GRAB_RETRIES} tries: {last}",
            command="printer vision",
            remediation=[
                "Check that the printer is on and its camera is enabled.",
                "A Snapmaker U1 writes the file only after camera.start_monitor "
                "(sent automatically unless --no-keepalive).",
            ],
        )


# ---- the U1's own detector, read from klippy.log ----------------------------------------------

_VERDICT = re.compile(
    r"^(\d\d):(\d\d):(\d\d)\.(\d+):\[defect_detection\] response info: (\{.*\})\s*$"
)


@dataclass
class NpuVerdict:
    """One answer of the printer's camera detector (Snapmaker unisrv, YOLO on the NPU)."""

    ts: float
    label: str  # "noodle", "residue", ...
    prob: float  # obj_probs: the confidence the firmware thresholds (0.83 low / 0.80 high)
    area: float  # obj_areas: fraction of the frame covered by detections
    count: int


def _line_ts(h: str, m: str, s: str, frac: str, now: float) -> float:
    """klippy.log carries only the time of day (printer clock, UTC on the U1): attach today's
    date, or yesterday's when that would land in the future (a line from before midnight)."""
    today = datetime.fromtimestamp(now, timezone.utc)
    t = today.replace(
        hour=int(h),
        minute=int(m),
        second=int(s),
        microsecond=int(frac.ljust(6, "0")[:6]),
    )
    if t.timestamp() > now + 300:
        t -= timedelta(days=1)
    return t.timestamp()


def parse_klippy_verdicts(text: str, now: float | None = None) -> list[NpuVerdict]:
    now = time.time() if now is None else now
    out: list[NpuVerdict] = []
    for line in text.splitlines():
        m = _VERDICT.match(line)
        if not m:
            continue
        try:
            info = ast.literal_eval(m.group(5))
        except (ValueError, SyntaxError):
            continue
        result = info.get("result") if isinstance(info, dict) else None
        if not isinstance(result, dict):
            continue
        ts = _line_ts(m.group(1), m.group(2), m.group(3), m.group(4), now)
        out.extend(
            NpuVerdict(
                ts,
                label,
                float(v.get("obj_probs") or 0.0),
                float(v.get("obj_areas") or 0.0),
                int(v.get("obj_cnts") or 0),
            )
            for label, v in result.items()
            if isinstance(v, dict) and "obj_probs" in v
        )
    return out


def _content_start(value: str | None) -> int | None:
    m = re.match(r"bytes (\d+)-\d+/\d+", value or "")
    return int(m.group(1)) if m else None


def _content_total(value: str | None) -> int | None:
    m = re.match(r"bytes (?:\d+-\d+|\*)/(\d+)", value or "")
    return int(m.group(1)) if m else None


@dataclass
class KlippyLogTap:
    """Incremental reader of klippy.log over Moonraker (`/server/files/logs/klippy.log`)."""

    host: str
    port: int = 7125
    timeout: float = 10.0
    offset: int | None = None  # next byte to read; None = start at the tail
    _partial: bytes = field(default=b"", repr=False)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/server/files/logs/klippy.log"

    def poll(self, now: float | None = None) -> list[NpuVerdict]:
        """Verdicts logged since the previous poll. A file shorter than our offset (rotation)
        restarts at byte 0."""
        rng = f"bytes=-{TAIL_BYTES}" if self.offset is None else f"bytes={self.offset}-"
        try:
            status, data, headers = _get(self.url, self.timeout, {"Range": rng})
        except OSError as exc:
            raise PrinterCommError(
                f"klippy.log from {self.host}: {exc}", command="printer vision"
            ) from exc
        if status == 416:  # nothing new, or the file rotated below our offset
            total = _content_total(headers.get("content-range"))
            if total is not None and self.offset is not None and total < self.offset:
                self.offset, self._partial = 0, b""
            return []
        if status not in (200, 206):
            raise PrinterCommError(
                f"klippy.log from {self.host}: HTTP {status}", command="printer vision"
            )
        start = (
            0 if status == 200 else (_content_start(headers.get("content-range")) or 0)
        )
        if (
            status == 200 and self.offset
        ):  # server ignored Range: skip what we already read
            data, start = data[self.offset :], self.offset
        end = start + len(data)
        if self.offset is None and start:  # tail read: the first line is cut, drop it
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        self.offset = end
        complete, _, self._partial = (self._partial + data).rpartition(b"\n")
        return parse_klippy_verdicts(complete.decode("utf-8", "replace"), now)


# ---- per-frame score -> failure level ---------------------------------------------------------


@dataclass
class Box:
    x1: float  # normalized 0..1 frame coordinates
    y1: float
    x2: float
    y2: float
    conf: float


@dataclass
class FrameScore:
    """What the pixel side (vision_detect.py) reports for one frame."""

    p: float  # spaghetti score: sum of kept detection confidences (Obico's per-frame p)
    boxes: list[Box] = field(default_factory=list)
    luma: float = (
        128.0  # mean brightness 0..255 (classic check: lights off / lens covered)
    )


@dataclass(frozen=True)
class TrackerParams:
    """Obico 1st-gen hyperparameters (backend/config/settings.py, tuned for one frame per 10 s)
    with two U1-measured changes:
    * `warmup`: Obico ignores the first 30 frames of a print; on the U1 the model scores at most
      0.34 (median 0.00) on 4,095 healthy frames, so 6 frames (1 min) is enough and a print
      that fails right after a resume is still caught.
    * `prior_frames`: Obico's long-term baseline is the printer's lifetime mean (7200 frames).
      A freshly started monitor has no lifetime, so its first frames WERE the baseline: started
      in front of spaghetti it would subtract the spaghetti from itself and never alert. The
      baseline therefore starts as if `prior_frames` frames had scored 0 (the U1's median)."""

    ewm_span: int = 12
    short_win: int = 310
    long_win: int = 7200
    low: float = 0.38
    high: float = 0.78
    short_multiple: float = 3.8
    escalate: float = 1.75
    warmup: int = 6
    prior_frames: int = 100


WARNING, CRITICAL = "warning", "critical"


@dataclass
class FailureTracker:
    params: TrackerParams = field(default_factory=TrackerParams)
    ewm: float = 0.0
    short_mean: float = 0.0
    long_mean: float = (
        0.0  # survives prints: the printer's own baseline (static clutter)
    )
    frames: int = 0  # frames in this print
    lifetime: int = -1  # -1 = not started: begins at params.prior_frames

    def __post_init__(self) -> None:
        if self.lifetime < 0:
            self.lifetime = self.params.prior_frames

    def reset_print(self) -> None:
        self.ewm = self.short_mean = 0.0
        self.frames = 0

    @staticmethod
    def _rolling(p: float, mean: float, count: int, win: int) -> float:
        return mean + (p - mean) / float(win if win <= count else count + 1)

    def update(self, p: float) -> str | None:
        """Feed one frame's score; returns None, WARNING or CRITICAL."""
        prm = self.params
        alpha = 2.0 / (prm.ewm_span + 1)
        self.ewm = p * alpha + self.ewm * (1 - alpha)
        self.short_mean = self._rolling(p, self.short_mean, self.frames, prm.short_win)
        self.long_mean = self._rolling(p, self.long_mean, self.lifetime, prm.long_win)
        self.frames += 1
        self.lifetime += 1
        if self._failing(prm.escalate):
            return CRITICAL
        return WARNING if self._failing(1.0) else None

    @property
    def adjusted(self) -> float:
        return self.ewm - self.long_mean

    def _failing(self, factor: float) -> bool:
        prm = self.params
        if self.frames <= prm.warmup:
            return False
        adj = self.adjusted / factor
        if adj < prm.low:
            return False
        return (
            adj > prm.high
            or adj > (self.short_mean - self.long_mean) * prm.short_multiple
        )


# ---- one monitoring step ----------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorLimits:
    stale_s: float = 120.0  # printing, but the frame has not changed for this long
    dark_luma: float = (
        20.0  # mean brightness below this while printing = cannot see the print
    )
    npu_threshold: float = 0.83  # the U1's own threshold at sensitivity "low"
    npu_quiet_s: float = 300.0  # no NPU hit for this long ends a noodle episode


@dataclass
class _Edge:
    """Edge-triggered condition: fires once per episode, re-arms when it clears."""

    active: str | None = None  # current level (None = clear)
    episode: int = 0

    def rise(self, level: str | None) -> bool:
        rank = {None: 0, WARNING: 1, CRITICAL: 2}
        fired = rank[level] > rank[self.active]
        if self.active is None and level is not None:
            self.episode += 1
        self.active = level
        return fired


@dataclass
class VisionMonitor:
    """Feed it frames (and optionally NPU verdicts); get watch Events back."""

    printer: str
    score: Callable[[bytes], FrameScore]
    annotate: Callable[[bytes, FrameScore], bytes] | None = None
    evidence_dir: Path | None = None
    limits: MonitorLimits = field(default_factory=MonitorLimits)
    tracker: FailureTracker = field(default_factory=FailureTracker)
    job: tuple[str, str | None] | None = None  # (state, filename) last seen
    last_change: float | None = None  # when the frame last changed
    last_digest: int | None = None
    last_npu: NpuVerdict | None = (
        None  # the strongest recent hit of the printer's detector
    )
    _spaghetti: _Edge = field(default_factory=_Edge)
    _stale: _Edge = field(default_factory=_Edge)
    _dark: _Edge = field(default_factory=_Edge)
    _npu: _Edge = field(default_factory=_Edge)

    def set_job(self, state: str, filename: str | None) -> None:
        """A new print (another file, or printing again after a stop) starts a fresh per-print
        history; pause/resume of the same file keeps it."""
        prev = self.job
        if state == PRINTING and (
            prev is None or prev[1] != filename or prev[0] not in (PRINTING, PAUSED)
        ):
            self.tracker.reset_print()
        self.job = (state, filename)

    @property
    def printing(self) -> bool:
        return self.job is not None and self.job[0] == PRINTING

    def step(
        self, frame: Frame | None, now: float | None = None
    ) -> tuple[FrameScore | None, list[Any]]:
        """Score one frame (None = the grab failed); returns (score, events). Spaghetti and
        camera checks run only while printing; idle frames just update the stale clock."""
        now = (frame.ts if frame is not None else time.time()) if now is None else now
        if frame is not None:
            digest = hash(frame.jpeg)
            if digest != self.last_digest or self.last_change is None:
                self.last_change, self.last_digest = now, digest
        elif self.last_change is None:
            self.last_change = now
        events: list[Any] = []
        if not self.printing:
            for edge in (self._stale, self._dark, self._spaghetti):
                edge.rise(None)
            self.last_change = self.last_digest = (
                None  # idle frames are not fetched: restart the clock
            )
            return None, events
        stale_for = now - self.last_change
        if self._stale.rise(WARNING if stale_for >= self.limits.stale_s else None):
            events.append(
                self._event(
                    "vision_camera_stale",
                    WARNING,
                    self._stale,
                    f"camera picture has not changed for {stale_for:.0f} s while printing; "
                    "spaghetti checks are blind until it updates",
                    frame,
                    None,
                    {"stale_s": round(stale_for)},
                )
            )
        if self._stale.active or frame is None:
            return None, events
        s = self.score(frame.jpeg)
        if self._dark.rise(WARNING if s.luma < self.limits.dark_luma else None):
            events.append(
                self._event(
                    "vision_camera_dark",
                    WARNING,
                    self._dark,
                    f"camera picture is dark (mean brightness {s.luma:.0f}/255) while printing; "
                    "is the chamber light off or the lens covered?",
                    frame,
                    s,
                    {"luma": round(s.luma, 1)},
                )
            )
        level = self.tracker.update(s.p)
        if self._spaghetti.rise(level) and level is not None:
            what = (
                "print looks like spaghetti"
                if level == CRITICAL
                else "possible spaghetti"
            )
            events.append(
                self._event(
                    "vision_spaghetti",
                    level,
                    self._spaghetti,
                    f"{what} on camera (score {s.p:.2f}, trend {self.tracker.adjusted:.2f}, "
                    f"{len(s.boxes)} region(s)); look at the frame and pause if it is real",
                    frame,
                    s,
                    {
                        "p": round(s.p, 3),
                        "trend": round(self.tracker.adjusted, 3),
                        "boxes": [vars(b) for b in s.boxes],
                    },
                )
            )
        return s, events

    def npu(self, verdicts: list[NpuVerdict], now: float) -> list[Any]:
        """The printer's own detector: one warning per noodle episode, while printing."""
        hits = [v for v in verdicts if v.prob >= self.limits.npu_threshold]
        if hits:
            self.last_npu = max(hits, key=lambda v: v.prob)
        recent = (
            self.last_npu is not None
            and now - self.last_npu.ts < self.limits.npu_quiet_s
        )
        if (
            not self._npu.rise(WARNING if self.printing and recent else None)
            or self.last_npu is None
        ):
            return []
        top = self.last_npu
        return [
            self._event(
                "printer_noodle_verdict",
                WARNING,
                self._npu,
                f"printer's own AI camera sees {top.label} (p {top.prob:.2f}, "
                f"{top.area * 100:.0f}% of the frame); the U1 pauses itself after several hits",
                None,
                None,
                {
                    "label": top.label,
                    "prob": top.prob,
                    "area": top.area,
                    "hits": len(hits),
                },
            )
        ]

    def _event(
        self,
        kind: str,
        severity: str,
        edge: _Edge,
        text: str,
        frame: Frame | None,
        score: FrameScore | None,
        data: dict[str, Any],
    ) -> Any:
        from printer_ops.watch import Event

        if frame is not None and self.evidence_dir is not None:
            data["frame"] = str(self._save(kind, frame, score))
        ts = frame.ts if frame is not None else time.time()
        return Event(
            self.printer,
            kind,
            severity,
            text,
            data,
            key=f"{self.printer}:{kind}:{edge.episode}:{severity}",
            ts=ts,
        )

    def _save(self, kind: str, frame: Frame, score: FrameScore | None) -> Path:
        assert self.evidence_dir is not None
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(frame.ts, timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        path = self.evidence_dir / f"{self.printer}-{stamp}-{kind}.jpg"
        data = (
            self.annotate(frame.jpeg, score)
            if self.annotate and score and score.boxes
            else frame.jpeg
        )
        path.write_bytes(data)
        return path
