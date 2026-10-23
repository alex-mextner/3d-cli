"""base.py — the printer-adapter contract shared by every `3d printer` backend.

WHAT: the normalized status model (`PrinterStatus`, `Slot`), the `Adapter` interface each
  protocol implements (Moonraker, Creality stock, later Bambu/PrusaLink/OctoPrint — epic
  https://github.com/alex-mextner/3d-cli/issues/90), the structured errors, and stdlib HTTP
  helpers (JSON + streamed multipart upload).

WHY a normalized state: safety rules are written once against it (refuse start while
  `printing`/`paused`/`busy`/`unknown`, issue #95) instead of per firmware. Each adapter
  keeps the firmware's own state string in `raw_state` and the full payload in `raw`.

INVARIANTS: stdlib only. Adapters never start a print from `upload`; every hardware write
  is a separate, explicitly named method.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from printer_ops.devices import Device
from urllib.parse import urlsplit

from errors import InvalidArgument, ThreeDError
from printer_ops.move_bounds import Limits

# Normalized states. BUSY_STATES block start/upload-overwrite; unknown counts as busy so a
# printer we cannot read is never treated as free.
IDLE, PRINTING, PAUSED, COMPLETE, CANCELLED, ERROR, BUSY, UNKNOWN = (
    "idle", "printing", "paused", "complete", "cancelled", "error", "busy", "unknown",
)
BUSY_STATES = frozenset({PRINTING, PAUSED, BUSY, UNKNOWN})
ACTIVE_JOB_STATES = frozenset({PRINTING, PAUSED, BUSY})

SAFE_NAME = re.compile(r"[A-Za-z0-9_.+-]+\.gcode")


class PrinterCommError(ThreeDError):
    """The printer could not be reached or answered something we cannot interpret. Exit 1."""

    exit_code = 1


class PrinterRefused(ThreeDError):
    """A safety rule refused the operation before anything was written to the printer. Exit 1."""

    exit_code = 1


class UnsupportedOperation(ThreeDError):
    """The adapter has no implementation for this operation. Exit 2."""

    exit_code = 2


@dataclass
class Slot:
    """One filament position: a toolhead (U1 T1..T4) or a single spool holder."""

    index: int              # 0-based; equals the G-code tool number Tn on the U1
    label: str              # what the printer UI shows (U1: T1..T4, 1-based)
    present: bool           # filament loaded / head usable
    color: str | None = None     # "#RRGGBB"
    material: str | None = None  # PLA, PETG, ...
    vendor: str | None = None
    sub_type: str | None = None


@dataclass
class PrinterStatus:
    state: str                       # one of the normalized states above
    raw_state: str                   # the firmware's own state string/code
    filename: str | None = None      # current/last job file
    progress: float | None = None    # 0..1
    temps: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)  # name -> (actual, target)
    message: str | None = None
    slots: list[Slot] = field(default_factory=list)
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["temps"] = {k: {"actual": a, "target": t} for k, (a, t) in self.temps.items()}
        return d


@dataclass
class RemoteFile:
    name: str
    size: int | None


@dataclass
class UploadResult:
    name: str
    size: int
    verified: bool
    note: str = ""


@dataclass(frozen=True)
class HeadFilament:
    """What a head should show it holds (the U1 touchscreen's per-head filament)."""

    color: str     # "#RRGGBB"
    material: str  # PLA, PETG, ... (the firmware's filament_type)
    vendor: str
    sub_type: str


@dataclass
class HeadWrite:
    """Outcome of `Adapter.set_head_filament`, every field read back from the printer."""

    index: int                       # 0-based head index (G-code tool number)
    extruder: str                    # Klipper object of that head (extruder, extruder1, ...)
    before: Slot                     # what the head showed before the write
    after: Slot                      # what it shows after (matches the request)
    pressure_advance: float | None   # the extruder's K after the write (the firmware may reset it)


@dataclass(frozen=True)
class ActiveSpool:
    """Moonraker's `[spoolman]` state: the ONE spool it books extruded filament to."""

    connected: bool                  # Moonraker reaches its Spoolman (`spoolman_connected`)
    spool_id: int | None             # the active Spoolman spool; None = none selected
    pending_reports: int | None      # spools with usage queued while Spoolman was unreachable


@dataclass(frozen=True)
class ActiveSpoolWrite:
    """Outcome of `Adapter.set_active_spool`; `after` is read back from the printer."""

    before: int | None
    after: int | None
    connected: bool                  # Moonraker's Spoolman link when the write was made (read just before it)
    pending_reports: int | None      # spools whose usage waits for Spoolman (see ActiveSpool)


@dataclass
class QueuedJob:
    """One job waiting in the printer's own job queue (not started yet)."""

    filename: str
    job_id: str | None = None
    waiting_s: float | None = None   # seconds since it was queued


@dataclass
class JobQueue:
    """The printer-side job queue: its state (ready/loading/starting/paused) and the waiting jobs."""

    state: str | None
    jobs: list[QueuedJob] = field(default_factory=list)


@dataclass
class JobRecord:
    """One finished (or interrupted) job of the printer's job history, newest first."""

    filename: str
    status: str                      # the firmware's own word: completed, cancelled, error, ...
    start_time: float | None = None  # unix seconds
    print_s: float | None = None     # time spent printing
    filament_mm: float | None = None
    job_id: str | None = None


@dataclass
class HistoryTotals:
    """Lifetime counters of the printer's job history."""

    jobs: int | None = None
    print_s: float | None = None
    filament_mm: float | None = None


class Adapter:
    """Interface every printer backend implements. Unimplemented ops raise UnsupportedOperation."""

    kind = "base"
    upload_while_busy = False  # may OTHER files be stored while a job runs? (never the active one)

    def __init__(self, name: str, host: str, port: int) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.configured_limits: Limits | None = None  # axis_limits from printer-devices.yaml
        self.trusted_macros: frozenset[str] = frozenset()  # trusted_macros from printer-devices.yaml
        self.device: Device | None = None  # its printer-devices.yaml entry (make_adapter sets it)

    def axis_limits(self) -> tuple[Limits | None, str]:
        """Real X/Y travel and where it came from; (None, reason) when unknown. Read-only."""
        if self.configured_limits is not None:
            return self.configured_limits, "printer-devices.yaml"
        return None, f"no axis_limits for {self.name} in printer-devices.yaml"

    def status(self) -> PrinterStatus:
        raise self._unsupported("status")

    def list_files(self, expect: tuple[str, int] | None = None) -> dict[str, RemoteFile]:
        """Remote G-code files by name. `expect=(name, size)` lets a slow lister stop early."""
        raise self._unsupported("list files")

    def queue(self) -> JobQueue:
        """The printer's own job queue. Read-only."""
        raise self._unsupported("a job queue")

    def history(self, limit: int) -> list[JobRecord]:
        """The `limit` newest jobs of the printer's job history, newest first. Read-only."""
        raise self._unsupported("job history")

    def history_totals(self) -> HistoryTotals:
        """Lifetime job count, print time and filament of the printer. Read-only."""
        raise self._unsupported("job history")

    def upload(self, local: Path, remote_name: str) -> UploadResult:
        raise self._unsupported("upload")

    def remote_sha256(self, remote_name: str) -> str | None:
        """SHA-256 of the stored file, read back from the printer; None when the firmware
        offers no way to read a file back (identity then rests on the hashed name + size)."""
        return None

    def start(self, remote_name: str) -> None:
        """Start a stored file. Returns only once the printer ACKNOWLEDGED the start; no
        acknowledgement raises PrinterCommError (outcome UNKNOWN, never resent)."""
        raise self._unsupported("start")

    def cancel(self) -> None:
        raise self._unsupported("cancel")

    def pause(self) -> None:
        """Request a native pause. The caller owns job binding and paused-state verification."""
        raise self._unsupported("pause")

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        """Run a G-code script (lines joined by newlines; the bed-clear acknowledgement in
        printer_ops/bed.py and the `3d calibrate photo` pose/light in topcam/photo.py). Returns
        once the printer ran it, waiting at most `timeout` seconds."""
        raise self._unsupported("run_gcode")

    def set_head_filament(self, index: int, filament: HeadFilament, *,
                          force_official: bool = False) -> HeadWrite:
        """Make head `index` show `filament` on the printer, verified by reading it back.
        `force_official` also overwrites a head whose filament came from an official RFID tag."""
        raise self._unsupported("writing a head's filament")

    def set_pressure_advance(self, index: int, advance: float, *,
                             smooth_time: float | None = None) -> float:
        """Set head `index`'s pressure advance; returns the K read back from the printer."""
        raise self._unsupported("setting pressure advance")

    def active_spool(self) -> ActiveSpool:
        """Moonraker's active spool and its Spoolman link. UnsupportedOperation when the printer
        has no `[spoolman]` (or the adapter has no such concept). Read-only."""
        raise self._unsupported("an active spool (no Moonraker [spoolman])")

    def set_active_spool(self, spool_id: int | None) -> ActiveSpoolWrite:
        """Make `spool_id` (None = clear) the spool the printer's own usage reporting books to,
        verified by reading it back."""
        raise self._unsupported("setting an active spool (no Moonraker [spoolman])")

    def _unsupported(self, op: str) -> UnsupportedOperation:
        return UnsupportedOperation(
            f"adapter '{self.kind}' (printer {self.name}) does not support {op}",
            command="printer",
            remediation=["See `3d printer --help` for what each adapter supports."],
        )

    def _comm_error(self, what: str, exc: BaseException | str) -> PrinterCommError:
        return PrinterCommError(
            f"{self.name} ({self.kind} {self.host}:{self.port}): {what}: {exc}",
            command="printer",
            remediation=[
                f"Check that the printer is on and reachable: ping {self.host}",
                "Check host/port in printer-devices.yaml (`3d printer list` shows the file).",
            ],
        )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_remote_name(local: Path, override: str | None = None) -> str:
    """Content-addressed remote file name: `<stem>-<first 16 hex of SHA-256>.gcode`.

    The stem is the --as override or the local basename with spaces -> '_' (Orca's
    safe_filename). Upload and start derive the same name from the same bytes, so `start`
    can never pick up a stale or overwritten file that merely has the same name and size —
    changed content gets a different name. 64 bits guard against accidental mismatches (a
    re-slice, a stale upload); where the firmware can return the file (Moonraker) start also
    compares the full SHA-256. Only [A-Za-z0-9_.+-] and a .gcode suffix."""
    name = override or local.name.replace(" ", "_")
    if not SAFE_NAME.fullmatch(name):
        raise InvalidArgument(
            "remote name", name, ["letters, digits, _ . + - ending in .gcode (no folders)"],
            command="printer", extra="Rename the file or pass --as NAME.gcode.",
        )
    tag = file_sha256(local)[:16]
    stem = name[: -len(".gcode")]
    return name if stem.endswith(f"-{tag}") else f"{stem}-{tag}.gcode"


# ---- stdlib HTTP helpers ------------------------------------------------------------------

def http_json(method: str, url: str, *, timeout: float = 10.0, body: bytes | None = None,
              headers: dict[str, str] | None = None) -> Any:
    """One HTTP request -> parsed JSON. Raises OSError/ValueError (adapters wrap them)."""
    status, data = http_request(method, url, timeout=timeout, body=body, headers=headers)
    if status >= 400:
        raise OSError(f"HTTP {status} from {method} {url}: {data[:300]!r}")
    return json.loads(data or b"null")


def http_request(method: str, url: str, *, timeout: float = 10.0, body: bytes | None = None,
                 headers: dict[str, str] | None = None, max_bytes: int | None = None) -> tuple[int, bytes]:
    """One HTTP request -> (status, body); `max_bytes` caps how much of the body is read."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read(max_bytes)
    finally:
        conn.close()


def http_upload(url: str, local: Path, remote_name: str, fields: dict[str, str], *,
                timeout: float = 300.0) -> tuple[int, bytes]:
    """Streamed multipart/form-data POST: `fields` first, then the file as part `file`.

    Streams in 1 MiB chunks so multi-hundred-MB G-code never sits in memory."""
    boundary = "3dcli" + uuid.uuid4().hex
    prefix = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    prefix += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{remote_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    size = os.path.getsize(local)
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout)
    try:
        conn.putrequest("POST", parts.path + (("?" + parts.query) if parts.query else ""))
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
        conn.endheaders()
        conn.send(prefix)
        with open(local, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                conn.send(chunk)
        conn.send(suffix)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()
