"""moonraker.py — Klipper/Moonraker adapter (Snapmaker U1 and any Moonraker printer).

PROTOCOL (Moonraker HTTP API, default port 7125), issue
https://github.com/alex-mextner/3d-cli/issues/92:
  status  GET  /printer/objects/query?webhooks&print_stats&virtual_sdcard&display_status&
               heater_bed&extruder..extruder3&print_task_config
  files   GET  /server/files/list?root=gcodes
  upload  POST /server/files/upload   multipart root=gcodes, print=false, file
  start   POST /printer/print/start?filename=<name>
  cancel  POST /printer/print/cancel

SNAPMAKER U1: Klipper object `print_task_config` carries what each of the 4 heads holds —
  the same fields Snapmaker Orca's "Sync filament" reads (SSWCP.cpp:1658-1767):
  filament_exist, filament_color_rgba ("RRGGBBAA"), filament_type, filament_vendor,
  filament_sub_type. Index i is G-code tool Ti and the printer's head label T(i+1). A head
  with filament_exist=false (our broken T2) is reported as an empty slot. Non-U1 Klipper
  printers simply lack the object and get no slots.

UPLOAD SAFETY: `print=false` is always sent and a response claiming print_started=true is
  reported loudly. A response timeout is not a failure by itself: Moonraker has been seen
  to time out after the whole file arrived (issue #68), so the file list is re-read and a
  matching size counts as verified; otherwise the outcome is UNKNOWN and never retried.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from printer_ops.base import (
    CANCELLED,
    COMPLETE,
    ERROR,
    IDLE,
    PAUSED,
    PRINTING,
    UNKNOWN,
    Adapter,
    PrinterCommError,
    PrinterStatus,
    RemoteFile,
    Slot,
    UploadResult,
    http_json,
    http_upload,
)
from printer_ops.move_bounds import Limits

_STATE = {
    "standby": IDLE,
    "printing": PRINTING,
    "paused": PAUSED,
    "complete": COMPLETE,
    "cancelled": CANCELLED,
    "error": ERROR,
}
_HEATERS = ("extruder", "extruder1", "extruder2", "extruder3", "heater_bed")
_QUERY = "webhooks&print_stats&virtual_sdcard&display_status&" + "&".join(_HEATERS) + "&print_task_config"


def _rgba_to_hex(value: Any) -> str | None:
    s = str(value or "").lstrip("#")
    if len(s) < 6:
        return None
    return "#" + s[:6].upper()


def _at(seq: Any, i: int) -> Any:
    return seq[i] if isinstance(seq, list) and i < len(seq) else None


def parse_slots(cfg: dict[str, Any]) -> list[Slot]:
    """U1 `print_task_config` -> one Slot per head (index = G-code tool number)."""
    exists = cfg.get("filament_exist")
    if not isinstance(exists, list):
        return []
    slots = []
    for i, present in enumerate(exists):
        slots.append(Slot(
            index=i,
            label=f"T{i + 1}",
            present=bool(present),
            color=_rgba_to_hex(_at(cfg.get("filament_color_rgba"), i)),
            material=_at(cfg.get("filament_type"), i),
            vendor=_at(cfg.get("filament_vendor"), i),
            sub_type=_at(cfg.get("filament_sub_type"), i),
        ))
    return slots


def parse_status(status: dict[str, Any]) -> PrinterStatus:
    """Moonraker `objects/query` status dict -> normalized PrinterStatus."""
    hooks = status.get("webhooks") or {}
    ps = status.get("print_stats") or {}
    klippy = hooks.get("state", "ready")
    raw_state = str(ps.get("state", "")) or "?"
    if klippy != "ready":
        # Klipper not ready (startup/shutdown/error): nothing about the job can be trusted.
        state, raw_state = UNKNOWN, f"klippy:{klippy}"
        message = hooks.get("state_message")
    else:
        state = _STATE.get(raw_state, UNKNOWN)
        message = ps.get("message") or None
    progress = (status.get("virtual_sdcard") or {}).get("progress")
    if progress is None:
        progress = (status.get("display_status") or {}).get("progress")
    temps: dict[str, tuple[float | None, float | None]] = {}
    for h in _HEATERS:
        obj = status.get(h)
        if isinstance(obj, dict) and obj:
            temps["bed" if h == "heater_bed" else h] = (obj.get("temperature"), obj.get("target"))
    return PrinterStatus(
        state=state,
        raw_state=raw_state,
        filename=ps.get("filename") or None,
        progress=float(progress) if progress is not None else None,
        temps=temps,
        message=message,
        slots=parse_slots(status.get("print_task_config") or {}),
        raw=status,
    )


class MoonrakerAdapter(Adapter):
    kind = "moonraker"
    upload_while_busy = True  # other files may be stored while printing; never the active one

    def __init__(self, name: str, host: str, port: int = 7125, *, timeout: float = 10.0,
                 upload_timeout: float = 600.0) -> None:
        super().__init__(name, host, port)
        self.timeout = timeout
        self.upload_timeout = upload_timeout

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get(self, path: str) -> Any:
        try:
            body = http_json("GET", self.base + path, timeout=self.timeout)
        except (OSError, ValueError) as exc:
            raise self._comm_error(f"GET {path}", exc) from exc
        return body.get("result", body) if isinstance(body, dict) else body

    def _post(self, path: str) -> Any:
        try:
            body = http_json("POST", self.base + path, timeout=self.timeout, body=b"")
        except (OSError, ValueError) as exc:
            raise self._comm_error(f"POST {path}", exc) from exc
        return body.get("result", body) if isinstance(body, dict) else body

    def query(self, objects: str) -> dict[str, Any]:
        """One read-only `objects/query` (e.g. "print_stats&filament_detect") -> its status dict."""
        result = self._get("/printer/objects/query?" + objects)
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, dict):
            raise self._comm_error("objects/query", f"no 'status' in reply: {str(result)[:200]}")
        return status

    def status(self) -> PrinterStatus:
        return parse_status(self.query(_QUERY))

    def axis_limits(self) -> tuple[Limits | None, str]:
        """Configured limits win; otherwise Klipper's own stepper_x/stepper_y position_min/max."""
        if self.configured_limits is not None:
            return self.configured_limits, "printer-devices.yaml"
        try:
            result = self._get("/printer/objects/query?configfile=settings")
        except PrinterCommError as exc:
            return None, f"Klipper config unreadable: {exc.message}"
        try:
            settings = result["status"]["configfile"]["settings"]
            sx, sy = settings["stepper_x"], settings["stepper_y"]
            lim = ((float(sx.get("position_min", 0.0)), float(sx["position_max"])),
                   (float(sy.get("position_min", 0.0)), float(sy["position_max"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None, "Klipper config has no stepper_x/stepper_y position_max"
        return lim, "Klipper stepper_x/stepper_y"

    def list_files(self, expect: tuple[str, int] | None = None) -> dict[str, RemoteFile]:
        result = self._get("/server/files/list?root=gcodes")
        files: dict[str, RemoteFile] = {}
        for e in result if isinstance(result, list) else []:
            path = e.get("path") or e.get("filename")
            if path:
                files[path] = RemoteFile(path, e.get("size"))
        return files

    def upload(self, local: Path, remote_name: str) -> UploadResult:
        size = local.stat().st_size
        url = self.base + "/server/files/upload"
        try:
            status, data = http_upload(url, local, remote_name, {"root": "gcodes", "print": "false"},
                                       timeout=self.upload_timeout)
        except TimeoutError as exc:
            return self._reconcile_after_timeout(remote_name, size, exc)
        except OSError as exc:
            raise self._comm_error("upload", exc) from exc
        if status not in (200, 201):
            raise self._comm_error("upload", f"HTTP {status}: {data[:300]!r}")
        try:
            body = json.loads(data)
        except ValueError as exc:
            raise self._comm_error("upload", f"non-JSON reply {data[:200]!r}") from exc
        result = body.get("result", body) if isinstance(body, dict) else {}
        if result.get("print_started") is True or result.get("print_queued") is True:
            raise PrinterCommError(
                f"{self.name}: Moonraker reports the upload STARTED or QUEUED a print although "
                "print=false was sent",
                command="printer",
                remediation=[f"Check the printer now: 3d printer status {self.name}",
                             f"Cancel if unintended: 3d printer cancel {self.name} --yes"],
            )
        remote = self.list_files().get(remote_name)
        if remote is None:
            raise self._comm_error("upload", f"{remote_name} missing from the file list after upload")
        if remote.size != size:
            raise self._comm_error("upload", f"size mismatch: local {size} B, printer {remote.size} B")
        return UploadResult(remote_name, size, verified=True)

    def _reconcile_after_timeout(self, remote_name: str, size: int, exc: BaseException) -> UploadResult:
        remote = self.list_files().get(remote_name)
        if remote is not None and remote.size == size:
            return UploadResult(remote_name, size, verified=True,
                                note="upload response timed out; size verified from the printer's file list")
        raise PrinterCommError(
            f"{self.name}: upload of {remote_name} timed out and the printer does not list it with "
            f"the expected size ({size} B; listed: {remote.size if remote else 'absent'}) — outcome UNKNOWN",
            command="printer",
            remediation=[f"Re-read before retrying: 3d printer status {self.name}",
                         "Retry the upload only after confirming the file is absent or incomplete."],
        ) from exc

    def remote_sha256(self, remote_name: str) -> str | None:
        """Read the stored file back (GET /server/files/gcodes/<name>) and hash it: the exact
        bytes `start` would print, not just a matching name and size."""
        url = self.base + "/server/files/gcodes/" + quote(remote_name, safe="/")
        parts = urlsplit(url)
        h = hashlib.sha256()
        conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=self.upload_timeout)
        try:
            conn.request("GET", parts.path)
            r = conn.getresponse()
            if r.status != 200:
                raise self._comm_error(f"read back {remote_name}", f"HTTP {r.status}")
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
        except (OSError, http.client.HTTPException) as exc:  # IncompleteRead is not an OSError
            raise self._comm_error(f"read back {remote_name}", exc) from exc
        finally:
            conn.close()
        return h.hexdigest()

    def start(self, remote_name: str) -> None:
        # Moonraker answers only after Klipper accepted the request: the HTTP 200 is the ack
        self._post("/printer/print/start?filename=" + quote(remote_name, safe=""))

    def cancel(self) -> None:
        self._post("/printer/print/cancel")


__all__ = ["MoonrakerAdapter", "parse_slots", "parse_status"]
