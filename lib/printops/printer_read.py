"""printer_read.py — READ-ONLY printer access for PrintOps slices (status, Moonraker history, G-code download).

ACCESSED VIA: printops.incident_context (auto-context of an incident: current job, layer, file
position) and printops.exposure (Moonraker job history → print-hours and grams per head).

INVARIANTS:
  - Only GET requests (Moonraker) and the adapter's own read-only `status()` (Creality: one ws
    status read). Nothing here can start, pause, heat, move or upload: PrintOps reads printers.
  - Offline fleet printers (other people's, D7) are never contacted: `device_for` returns no
    Device for them.
  - A G-code download is streamed to a temporary file and renamed into place only when its
    size equals the size the printer reported, so the cache never holds a truncated file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from errors import ThreeDError

if TYPE_CHECKING:
    from printer_ops.base import PrinterStatus
    from printer_ops.devices import Device
    from printops.fleet import FleetPrinter

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
TIMEOUT_S = 10.0


class PrinterReadError(RuntimeError):
    """The printer could not be read (unreachable, unexpected reply, unsupported adapter)."""


@dataclass(frozen=True)
class FleetEntry:
    printer: FleetPrinter
    device: Device | None  # None: offline (never contacted) or not addressable


def device_for(name: str) -> FleetEntry:
    """The fleet entry of `name` and, when 3d may contact it, its Device. LookupError if the
    fleet has no such printer; PrinterReadError when fleet/printers.yaml is unusable."""
    from printer_ops.devices import fleet_devices

    from printops.fleet import Fleet, FleetError, load_fleet

    try:
        fleet = load_fleet()
    except FleetError as e:
        raise PrinterReadError(f"fleet registry unreadable: {e}") from e
    if fleet is None:
        raise PrinterReadError("no fleet registry (fleet/printers.yaml)")
    printer = fleet.printers.get(name)
    if printer is None:
        raise LookupError(f"unknown printer {name!r}; fleet has: {', '.join(sorted(fleet.printers))}")
    if not printer.contactable:
        return FleetEntry(printer, None)
    try:
        device = fleet_devices(Fleet(fleet.path, {name: printer})).get(name)
    except ThreeDError as e:
        raise PrinterReadError(f"{name}: {e.message}") from e
    return FleetEntry(printer, device)


def status(device: Device) -> PrinterStatus:
    """The adapter's read-only status (Moonraker: one objects/query GET)."""
    from printer_ops import make_adapter

    try:
        return make_adapter(device).status()
    except ThreeDError as e:
        raise PrinterReadError(f"{device.name}: {e.message}") from e


def _base(device: Device) -> str:
    if device.adapter != "moonraker":
        raise PrinterReadError(f"{device.name}: adapter {device.adapter} has no Moonraker API")
    return f"http://{device.host}:{device.port}"


def moonraker_get(device: Device, path: str) -> Any:
    """GET a Moonraker endpoint; returns its `result`."""
    from printer_ops.base import http_json

    url = _base(device) + path
    try:
        body = http_json("GET", url, timeout=TIMEOUT_S)
    except (OSError, ValueError) as e:
        raise PrinterReadError(f"{device.name}: GET {path} failed: {e}") from e
    if not isinstance(body, dict) or "result" not in body:
        raise PrinterReadError(f"{device.name}: GET {path}: unexpected reply {str(body)[:200]!r}")
    return body["result"]


def download_gcode(device: Device, filename: str, dest: Path, *, size: int | None) -> Path:
    """Stream `gcodes/<filename>` from Moonraker into `dest` (GET /server/files/gcodes/…)."""
    url = f"{_base(device)}/server/files/gcodes/{quote(filename)}"
    if size is not None and size > MAX_DOWNLOAD_BYTES:
        raise PrinterReadError(f"{filename} is {size} bytes, over the {MAX_DOWNLOAD_BYTES} byte download limit")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dl-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=TIMEOUT_S * 6) as resp:  # noqa: S310 — fleet host
            shutil.copyfileobj(resp, out, 1024 * 1024)
        got = Path(tmp).stat().st_size
        if size is not None and got != size:
            raise PrinterReadError(f"{filename}: downloaded {got} bytes, the printer reports {size}")
        os.replace(tmp, dest)
    except (OSError, urllib.error.URLError) as e:
        raise PrinterReadError(f"{device.name}: download of {filename} failed: {e}") from e
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest
