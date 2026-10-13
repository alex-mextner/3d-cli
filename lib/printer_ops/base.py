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

import http.client
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from errors import InvalidArgument, ThreeDError

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


class Adapter:
    """Interface every printer backend implements. Unimplemented ops raise UnsupportedOperation."""

    kind = "base"
    upload_while_busy = False  # may OTHER files be stored while a job runs? (never the active one)

    def __init__(self, name: str, host: str, port: int) -> None:
        self.name = name
        self.host = host
        self.port = port

    def status(self) -> PrinterStatus:
        raise self._unsupported("status")

    def list_files(self, expect: tuple[str, int] | None = None) -> dict[str, RemoteFile]:
        """Remote G-code files by name. `expect=(name, size)` lets a slow lister stop early."""
        raise self._unsupported("list files")

    def upload(self, local: Path, remote_name: str) -> UploadResult:
        raise self._unsupported("upload")

    def start(self, remote_name: str) -> None:
        raise self._unsupported("start")

    def cancel(self) -> None:
        raise self._unsupported("cancel")

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


def safe_remote_name(local: Path, override: str | None = None) -> str:
    """Remote file name: --as override or the local basename with spaces -> '_' (Orca's
    safe_filename). Only [A-Za-z0-9_.+-] and a .gcode suffix — no paths, no query chars."""
    name = override or local.name.replace(" ", "_")
    if not SAFE_NAME.fullmatch(name):
        raise InvalidArgument(
            "remote name", name, ["letters, digits, _ . + - ending in .gcode (no folders)"],
            command="printer", extra="Rename the file or pass --as NAME.gcode.",
        )
    return name


# ---- stdlib HTTP helpers ------------------------------------------------------------------

def http_json(method: str, url: str, *, timeout: float = 10.0, body: bytes | None = None,
              headers: dict[str, str] | None = None) -> Any:
    """One HTTP request -> parsed JSON. Raises OSError/ValueError (adapters wrap them)."""
    status, data = http_request(method, url, timeout=timeout, body=body, headers=headers)
    if status >= 400:
        raise OSError(f"HTTP {status} from {method} {url}: {data[:300]!r}")
    return json.loads(data or b"null")


def http_request(method: str, url: str, *, timeout: float = 10.0, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read()
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
