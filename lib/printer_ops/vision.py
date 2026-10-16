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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
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


# The K1 SE's mjpg-streamer (hardware encoder) pads frames with ~25 filler bytes after EOI.
EOI_TAIL_MAX = 64


def _jpeg_end(data: bytes) -> int | None:
    """Index just past the EOI that closes the image, walking the marker segments by length
    (so FFD9/FFDA bytes inside APP/COM payloads or an EXIF thumbnail never count), or None when
    the data ends before EOI. Scan data byte-stuffs 0xFF, so there FFxx is a real marker."""
    i, n, scanned = 2, len(data), False
    while i + 1 < n:
        if data[i] != 0xFF:
            if not scanned:
                return None  # garbage between header segments
            i = data.find(b"\xff", i)  # skip entropy-coded scan data to the next 0xFF
            if i == -1:
                return None
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte before a marker
            i += 1
        elif marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD7:  # stuffed 0xFF / TEM / RSTn
            i += 2
        elif marker == 0xD9:
            return i + 2 if scanned else None
        elif i + 3 >= n:
            return None
        else:
            seg_end = i + 2 + int.from_bytes(data[i + 2 : i + 4], "big")
            if seg_end > n:
                return None
            scanned = scanned or marker == 0xDA
            i = seg_end
    return None


def jpeg_frame(data: bytes, tail_max: int = 0) -> bytes | None:
    """The JPEG cut at its EOI, or None for a half-written monitor.jpg / non-JPEG body.

    At most `tail_max` filler bytes (beyond a zero tail) may follow EOI. Keep it 0 for a file
    rewritten in place: there an old padded frame's EOI+tail could close a half-new hybrid.
    The filler is dropped so a frozen camera with varying padding hashes as the same frame."""
    if len(data) <= 4 or data[:2] != b"\xff\xd8":
        return None
    end = _jpeg_end(data)
    # monitor.jpg rewritten in place can carry a zero tail of any length
    if end is None or len(data.rstrip(b"\x00")) - end > tail_max:
        return None
    return data[:end]


def is_complete_jpeg(data: bytes, tail_max: int = 0) -> bool:
    return jpeg_frame(data, tail_max) is not None


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
    except http.client.HTTPException as exc:
        # IncompleteRead (monitor.jpg rewritten mid-response) etc. are not OSErrors; callers
        # retry/wrap OSError only, so convert here instead of crashing the live loop.
        raise OSError(f"{type(exc).__name__}: {exc}") from exc
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
        # A camera streamer (mjpg-streamer, ustreamer, go2rtc) sends whole in-memory frames, so
        # encoder padding is safe there; a file under Moonraker's /server/files/ (the U1
        # monitor.jpg) is rewritten in place, so a tail could belong to the previous frame.
        in_place = "/server/files/" in urlsplit(self.url).path  # also behind a proxy prefix
        self.tail_max = 0 if in_place else EOI_TAIL_MAX
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
            frame = jpeg_frame(data, self.tail_max)
            if frame is None:
                last = f"incomplete JPEG ({len(data)} bytes)"
                continue
            return Frame(ts, frame, _http_date(headers.get("last-modified")))
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
    """klippy.log carries only the time of day (printer clock, UTC on the U1): attach the date of
    `now` (the printer's clock), or the day before when that would land in the future (a line
    from before midnight)."""
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
        for label, v in result.items():
            if not isinstance(v, dict) or "obj_probs" not in v:
                continue
            try:  # firmware-controlled values: a malformed one skips the entry, not the loop
                out.append(
                    NpuVerdict(
                        ts,
                        str(label).strip().lower(),  # compared against fixed label names
                        float(v.get("obj_probs") or 0.0),
                        float(v.get("obj_areas") or 0.0),
                        int(v.get("obj_cnts") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
    # The log is chronological; only the date is guessed. A line dated after a later line is
    # from an earlier day (a tail spanning midnight or a day of silence), not "just now".
    later = None
    for i in range(len(out) - 1, -1, -1):
        while later is not None and out[i].ts > later + 1:
            out[i] = replace(out[i], ts=out[i].ts - 86400)
        later = out[i].ts
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
        # klippy.log times are the printer's clock; the monitor compares them with the host's.
        # Date the lines by the printer's own `Date:` header and shift them onto the host clock,
        # so a skewed printer clock neither hides nor replays verdicts.
        host_now = time.time() if now is None else now
        printer_now = _http_date(headers.get("date"))
        skew = 0.0 if printer_now is None or now is not None else printer_now - host_now
        verdicts = parse_klippy_verdicts(
            complete.decode("utf-8", "replace"), host_now + skew
        )
        return [replace(v, ts=v.ts - skew) for v in verdicts] if skew else verdicts


@dataclass(frozen=True)
class PrintContext:
    """Where the print is, for the camera masks (vision_geometry.py): the current file, gcode Z,
    toolhead XY and the object footprints Klipper's exclude_object knows (convex hulls, mm)."""

    filename: str | None
    z: float
    x: float
    y: float
    objects: tuple[tuple[tuple[float, float], ...], ...] = ()


@dataclass
class ContextTap:
    """One read-only Moonraker query per frame: gcode_move, toolhead, print_stats, exclude_object."""

    host: str
    port: int = 7125
    timeout: float = 5.0

    @property
    def url(self) -> str:
        return (
            f"http://{self.host}:{self.port}/printer/objects/query?"
            "gcode_move=gcode_position&toolhead=position&print_stats=filename"
            "&exclude_object=objects"
        )

    def poll(self) -> PrintContext:
        try:
            status, data, _ = _get(self.url, self.timeout)
            if status != 200:
                raise OSError(f"HTTP {status}")
            st = json.loads(data)["result"]["status"]
            gz = st["gcode_move"]["gcode_position"]
            pos = st["toolhead"]["position"]
            objects = tuple(
                tuple((float(x), float(y)) for x, y in o.get("polygon") or [])
                for o in (st.get("exclude_object") or {}).get("objects") or []
            )
            return PrintContext(
                (st.get("print_stats") or {}).get("filename"),
                float(gz[2]),
                float(pos[0]),
                float(pos[1]),
                tuple(o for o in objects if len(o) >= 3),
            )
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise PrinterCommError(
                f"print position from {self.host}: {exc}", command="printer vision"
            ) from exc


# ---- per-frame score -> failure level ---------------------------------------------------------


@dataclass
class Box:
    x1: float  # normalized 0..1 frame coordinates
    y1: float
    x2: float
    y2: float
    conf: float


@dataclass
class Finding:
    """A non-spaghetti detector result for one frame (vision_scene.py: foreign object, moved part,
    stalled growth). Present while the condition holds; VisionMonitor turns it into one event per
    episode. `image`: an extra evidence JPEG saved next to the frame."""

    kind: str
    severity: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    image: bytes | None = None


@dataclass
class FrameScore:
    """What the pixel side (vision_detect.py) reports for one frame."""

    p: float  # spaghetti score: sum of kept detection confidences (Obico's per-frame p)
    boxes: list[Box] = field(default_factory=list)
    luma: float = (
        128.0  # mean brightness 0..255 (classic check: lights off / lens covered)
    )
    # Stage 2 (vision_verify.py): failure probability of the strongest boxes (in `boxes` order,
    # which is strongest first) and q = sum(conf x probability). q None = no verifier ran, so
    # the tracker falls back to the raw p.
    probs: list[float] = field(default_factory=list)
    q: float | None = None
    masked: int = 0  # stage-1 boxes dropped by the static mask / bed-volume ROI
    findings: list[Finding] = field(default_factory=list)

    @property
    def signal(self) -> float:
        """What the trend tracker consumes: the verified score when there is one."""
        return self.p if self.q is None else self.q


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


INFO, WARNING, CRITICAL = "info", "warning", "critical"
# Verified q below this share of the raw p = the verifier disagrees with the detector. Measured
# 2026-10-14 on the U1: real spaghetti q/p 0.6-1.0, speckled-wall false positives 0.02-0.17.
VETO_RATIO = 0.3


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


# Every label the U1 camera detector (unisrv `camera.detect_capture`) reports, from Snapmaker
# u1-klipper defect_detection.py and the U1's klippy.log (2026-10-16):
#   print-time check, request labels ['noodle'] (+ 'residue' when enabled): response_callback
#     pauses on noodle (CONFIRM_NOODLE, exception code 2) and residue (code 3);
#   start-of-print clean-bed check (DEFECT_DETECTION_DETECT_BED), request ['item']: the answer
#     also carries nozzle, legend_text and extrusion_line; only item decides (dirty bed, code 1);
#   nozzle check, request ['nozzle']: detect_dirty_nozzle reads a HIGH prob as "found, clean".
# A dirty-bed `item` makes the firmware pause at once (exception code 1), which `printer watch`
# reports as an exception; this event only adds the detector's own numbers.
class NpuLabel(NamedTuple):
    defect: bool
    meaning: str
    firmware: str  # what the U1 does about it, for the event text


NPU_LABELS: dict[str, NpuLabel] = {
    "noodle": NpuLabel(True, "loose filament (spaghetti)", "the U1 pauses itself after several hits"),
    "residue": NpuLabel(True, "residue on the print", "the U1 pauses itself after several hits"),
    "item": NpuLabel(True, "a foreign object on the bed", "the U1 pauses the start (dirty bed)"),
    "nozzle": NpuLabel(False, "the nozzle itself", "no action"),
    "legend_text": NpuLabel(False, "the markings printed on the build plate", "no action"),
    "extrusion_line": NpuLabel(False, "a purge/extrusion line on the bed", "no action"),
}
UNKNOWN_NPU_LABEL = NpuLabel(True, "a label this firmware version did not document", "check the camera")


@dataclass(frozen=True)
class MonitorLimits:
    stale_s: float = 120.0  # printing, but the frame has not changed for this long
    dark_luma: float = (
        20.0  # mean brightness below this while printing = cannot see the print
    )
    npu_threshold: float = 0.83  # the U1's own threshold at sensitivity "low"
    npu_quiet_s: float = 300.0  # no NPU hit for this long ends a noodle episode
    # NPU labels that are not defects (NPU_LABELS); unknown labels still count, so a defect
    # class added by a firmware update reaches the agent.
    npu_ignored_labels: frozenset[str] = frozenset(
        label for label, spec in NPU_LABELS.items() if not spec.defect
    )


_RANK: dict[str | None, int] = {None: 0, INFO: 1, WARNING: 2, CRITICAL: 3}


@dataclass
class _Edge:
    """Edge-triggered condition: fires once per episode, re-arms when it clears."""

    active: str | None = None  # current level (None = clear)
    episode: int = 0

    def rise(self, level: str | None) -> bool:
        fired = _RANK[level] > _RANK[self.active]
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
    # on FrameScore.signal (the verified score when stage 2 runs)
    tracker: FailureTracker = field(default_factory=FailureTracker)
    # The raw stage-1 trend, kept only to report what the verifier vetoed (info events).
    raw_tracker: FailureTracker = field(default_factory=FailureTracker)
    job: tuple[str, str | None] | None = None  # (state, filename) last seen
    last_change: float | None = None  # when the frame last changed
    last_digest: int | None = None
    last_npu: NpuVerdict | None = (
        None  # the strongest recent hit of the printer's detector
    )
    _spaghetti: _Edge = field(default_factory=_Edge)
    _vetoed: _Edge = field(default_factory=_Edge)
    _stale: _Edge = field(default_factory=_Edge)
    _dark: _Edge = field(default_factory=_Edge)
    _npu: _Edge = field(default_factory=_Edge)
    _findings: dict[str, _Edge] = field(default_factory=dict)  # per Finding.kind

    def set_job(self, state: str, filename: str | None) -> bool:
        """A new print (another file, or printing again after a stop) starts a fresh per-print
        history; pause/resume of the same file keeps it. -> True when a new print started, so
        the caller can reset its own per-print state (the scorer's bed-change references)."""
        prev = self.job
        started = state == PRINTING and (
            prev is None or prev[1] != filename or prev[0] not in (PRINTING, PAUSED)
        )
        if started:
            self.tracker.reset_print()
            self.raw_tracker.reset_print()
        self.job = (state, filename)
        return started

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
            for edge in (self._stale, self._dark, self._spaghetti, self._vetoed):
                edge.rise(None)
            for edge in self._findings.values():
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
        level = self.tracker.update(s.signal)
        raw = self.raw_tracker.update(s.p) if s.q is not None else None
        data = {
            "p": round(s.p, 3),
            "q": None if s.q is None else round(s.q, 3),
            "trend": round(self.tracker.adjusted, 3),
            "boxes": [
                {**{k: round(v, 4) for k, v in vars(b).items()}, "verified": prob}
                for b, prob in zip(
                    s.boxes,
                    [round(x, 3) for x in s.probs]
                    + [None] * (len(s.boxes) - len(s.probs)),
                    strict=True,
                )
            ],
        }
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
                    f"{what} on camera ({self._describe(s)}, trend {self.tracker.adjusted:.2f}); "
                    "look at the frame and pause if it is real",
                    frame,
                    s,
                    data,
                )
            )
        # The detector alone would have alerted but the verifier disagrees (it confirms less
        # than VETO_RATIO of the raw score): an info event (to subscribers that asked for info)
        # so an agent can audit the veto; never a warning. A verified failure whose trend just
        # lags the raw one is not a veto.
        vetoed = (
            INFO
            if raw is not None
            and level is None
            and s.q is not None
            and s.q < VETO_RATIO * s.p
            else None
        )
        if self._vetoed.rise(vetoed) and vetoed is not None:
            events.append(
                self._event(
                    "vision_spaghetti_vetoed",
                    INFO,
                    self._vetoed,
                    f"the spaghetti detector fires but the verifier sees no loose filament "
                    f"({self._describe(s)}, raw trend {self.raw_tracker.adjusted:.2f}); "
                    "probably texture, not a failure",
                    frame,
                    s,
                    {**data, "raw_trend": round(self.raw_tracker.adjusted, 3)},
                )
            )
        events += self._finding_events(frame, s)
        return s, events

    def _finding_events(self, frame: Frame, s: FrameScore) -> list[Any]:
        """One event per episode of each Finding kind; a kind absent from this frame clears."""
        active = {f.kind: f for f in s.findings}
        out = []
        for kind in sorted(set(self._findings) | set(active)):
            edge = self._findings.setdefault(kind, _Edge())
            f = active.get(kind)
            if edge.rise(None if f is None else f.severity) and f is not None:
                out.append(
                    self._event(
                        kind, f.severity, edge, f.text, frame, s, dict(f.data), f.image
                    )
                )
        return out

    @staticmethod
    def _describe(s: FrameScore) -> str:
        if s.q is None:
            return f"score {s.p:.2f}, {len(s.boxes)} region(s)"
        sure = sum(x >= 0.5 for x in s.probs)
        return (
            f"verified score {s.q:.2f} of raw {s.p:.2f}: {sure} of {len(s.probs)} checked "
            "region(s) look like loose filament"
        )

    def npu(self, verdicts: list[NpuVerdict], now: float) -> list[Any]:
        """The printer's own detector: one warning per noodle episode, while printing."""
        ignored = self.limits.npu_ignored_labels
        hits = [v for v in verdicts if v.label not in ignored and v.prob >= self.limits.npu_threshold]
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
        spec = NPU_LABELS.get(top.label, UNKNOWN_NPU_LABEL)
        return [
            self._event(
                "printer_noodle_verdict",
                WARNING,
                self._npu,
                f"printer's own AI camera sees {top.label}: {spec.meaning} "
                f"(p {top.prob:.2f}, {top.area * 100:.0f}% of the frame); {spec.firmware}",
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
        image: bytes | None = None,
    ) -> Any:
        from printer_ops.watch import Event

        if frame is not None and self.evidence_dir is not None:
            data["frame"] = str(self._save(kind, frame, score))
            if (
                image is not None
            ):  # e.g. reference | now | changed pixels, next to the frame
                path = Path(data["frame"]).with_suffix(".evidence.jpg")
                path.write_bytes(image)
                data["evidence"] = str(path)
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
