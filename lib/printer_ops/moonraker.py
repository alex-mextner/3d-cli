"""moonraker.py — Klipper/Moonraker adapter (Snapmaker U1 and any Moonraker printer).

PROTOCOL (Moonraker HTTP API, default port 7125), issue
https://github.com/alex-mextner/3d-cli/issues/92:
  status  GET  /printer/objects/query?webhooks&print_stats&virtual_sdcard&display_status&
               heater_bed&extruder..extruder3&print_task_config
  files   GET  /server/files/list?root=gcodes
  upload  POST /server/files/upload   multipart root=gcodes, print=false, file
  start   POST /printer/print/start?filename=<name>
  cancel  POST /printer/print/cancel
  queue   GET  /server/job_queue/status        (read-only: `3d printers show`)
  history GET  /server/history/list?limit=N&order=desc, /server/history/totals   (read-only: same)
  gcode   POST /printer/gcode/script?script=<line>   (the bed-clear ack, printer_ops/bed.py; the
               head-filament write and pressure advance below)

SNAPMAKER U1: Klipper object `print_task_config` carries what each of the 4 heads holds —
  the same fields Snapmaker Orca's "Sync filament" reads (SSWCP.cpp:1658-1767):
  filament_exist, filament_color_rgba ("RRGGBBAA"), filament_type, filament_vendor,
  filament_sub_type. Index i is G-code tool Ti and the printer's head label T(i+1). A head
  with filament_exist=false (our broken T2) is reported as an empty slot. Non-U1 Klipper
  printers simply lack the object and get no slots.

HEAD FILAMENT WRITE (U1, issue #400): `set_head_filament` sends ONE line
  `SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=<i> VENDOR="…" FILAMENT_TYPE="…" FILAMENT_SUBTYPE="…"
  FILAMENT_COLOR_RGBA=RRGGBBFF [FORCE=1]` — the firmware command the touchscreen and paxx12's
  filament UI use (Snapmaker/u1-klipper@10f2f69 klippy/extras/print_task_config.py:122-123,
  585-711; no webhook endpoint exists for it, and it is missing from /printer/gcode/help only
  because it registers no help text). FILAMENT_TYPE needs VENDOR and FILAMENT_SUBTYPE (:618-620).
  Firmware behaviour we design around:
  - a head whose filament came from an official Snapmaker RFID tag refuses without FORCE=1
    (:607); we check `filament_official` first and send FORCE=1 only when asked to;
  - the write persists to print_task.json and then runs `FLOW_RESET_K EXTRUDER=<i>` (:708):
    the head's pressure advance drops to the firmware's default for vendor/type/sub-type
    (flow_calibrator.py:574-595). So the K is read back after every write, and the caller
    re-applies a calibrated K with `set_pressure_advance` (SET_PRESSURE_ADVANCE, read back).
    That K lives until Klipper restarts: on klippy:ready the firmware re-applies its own
    stored K (flow_calibrator.py:296-301, 336-337);
  - Klipper ends a command's arguments at `#`, `;` or `*` and quotes cannot be escaped
    (gcode.py:279-311), so text values carrying them are refused before anything is sent;
  - never while printing or paused, nor when Klipper is not ready: FLOW_RESET_K would change
    K mid-print.

UPLOAD SAFETY: `print=false` is always sent and a response claiming print_started=true is
  reported loudly. A response timeout is not a failure by itself: Moonraker has been seen
  to time out after the whole file arrived (issue #68), so the file list is re-read and a
  matching size counts as verified; otherwise the outcome is UNKNOWN and never retried.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
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
    HeadFilament,
    HeadWrite,
    HistoryTotals,
    JobQueue,
    JobRecord,
    PrinterCommError,
    PrinterRefused,
    PrinterStatus,
    QueuedJob,
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
_T = TypeVar("_T")
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


# Characters that end or break a Klipper extended-command argument (gcode.py:279-311).
_UNSAFE = re.compile(r"[\"'#;*\x00-\x1f\x7f]")
_HEX6 = re.compile(r"[0-9A-F]{6}")
MAX_TEXT = 64  # longer vendor/material names are a data error, not something to send
PA_TOLERANCE = 1e-6


def extruder_name(index: int) -> str:
    """Klipper object of head `index`: extruder, extruder1, extruder2, ..."""
    return "extruder" if index == 0 else f"extruder{index}"


def filament_config_gcode(index: int, filament: HeadFilament, *, force_official: bool = False) -> str:
    """The one `SET_PRINT_FILAMENT_CONFIG` line for head `index`. ValueError when a value cannot
    travel in a Klipper argument (nothing is sent then)."""
    color = filament.color.strip().lstrip("#").upper()
    if not _HEX6.fullmatch(color):
        raise ValueError(f"colour must be #RRGGBB, got {filament.color!r}")
    texts = {"vendor": filament.vendor, "material": filament.material, "sub-type": filament.sub_type}
    for what, value in texts.items():
        if not value.strip():
            raise ValueError(f"{what} is empty")
        if len(value) > MAX_TEXT:
            raise ValueError(f"{what} is longer than {MAX_TEXT} characters: {value[:MAX_TEXT]!r}…")
        if _UNSAFE.search(value):
            raise ValueError(f"{what} {value!r} holds a character Klipper G-code cannot carry (quote # ; * or a control character)")
    line = (
        f'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER={index} VENDOR="{filament.vendor}" '
        f'FILAMENT_TYPE="{filament.material}" FILAMENT_SUBTYPE="{filament.sub_type}" '
        f"FILAMENT_COLOR_RGBA={color}FF"
    )
    return line + " FORCE=1" if force_official else line


def _differs(after: Slot, want: HeadFilament) -> list[str]:
    pairs = (
        ("colour", after.color, "#" + want.color.strip().lstrip("#").upper()),
        ("material", after.material, want.material),
        ("vendor", after.vendor, want.vendor),
        ("sub-type", after.sub_type, want.sub_type),
    )
    return [f"{what} {got!r} (sent {sent!r})" for what, got, sent in pairs if got != sent]


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


def _num(value: Any) -> float | None:
    """A finite JSON number (not a bool, not NaN/Infinity) as float, else None."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value) if ok and math.isfinite(value) else None


def parse_queue(result: Any) -> JobQueue:
    """Moonraker `/server/job_queue/status` result -> JobQueue."""
    if not isinstance(result, dict) or not isinstance(result.get("queued_jobs"), list):
        raise ValueError(f"job_queue/status: no 'queued_jobs' list in {str(result)[:120]!r}")
    jobs = [
        QueuedJob(str(j["filename"]), str(j["job_id"]) if j.get("job_id") is not None else None,
                  _num(j.get("time_in_queue")))
        for j in result["queued_jobs"]
        if isinstance(j, dict) and j.get("filename")
    ]
    state = result.get("queue_state")
    return JobQueue(str(state) if state is not None else None, jobs)


def parse_history(result: Any) -> list[JobRecord]:
    """Moonraker `/server/history/list` result -> JobRecords in the order the printer sent them."""
    if not isinstance(result, dict) or not isinstance(result.get("jobs"), list):
        raise ValueError(f"history/list: no 'jobs' list in {str(result)[:120]!r}")
    return [
        JobRecord(
            filename=str(j.get("filename") or "?"),
            status=str(j.get("status") or "?"),
            start_time=_num(j.get("start_time")),
            print_s=_num(j.get("print_duration")),
            filament_mm=_num(j.get("filament_used")),
            job_id=str(j["job_id"]) if j.get("job_id") is not None else None,
        )
        for j in result["jobs"]
        if isinstance(j, dict)
    ]


def parse_totals(result: Any) -> HistoryTotals:
    """Moonraker `/server/history/totals` result -> HistoryTotals."""
    totals = result.get("job_totals") if isinstance(result, dict) else None
    if not isinstance(totals, dict):
        raise ValueError(f"history/totals: no 'job_totals' in {str(result)[:120]!r}")
    jobs = _num(totals.get("total_jobs"))
    return HistoryTotals(
        jobs=int(jobs) if jobs is not None else None,
        print_s=_num(totals.get("total_print_time")),
        filament_mm=_num(totals.get("total_filament_used")),
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

    def _parsed(self, path: str, parse: Callable[[Any], _T]) -> _T:
        try:
            return parse(self._get(path))
        except ValueError as exc:
            raise self._comm_error(f"GET {path}", exc) from exc

    def queue(self) -> JobQueue:
        return self._parsed("/server/job_queue/status", parse_queue)

    def history(self, limit: int) -> list[JobRecord]:
        return self._parsed(f"/server/history/list?limit={int(limit)}&order=desc", parse_history)

    def history_totals(self) -> HistoryTotals:
        return self._parsed("/server/history/totals", parse_totals)

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

    def pause(self) -> None:
        self._post("/printer/print/pause")

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        """Run G-code lines (newline-separated) through `/printer/gcode/script`. Moonraker
        answers when Klipper finished them (an `M400` at the end waits for the moves; a long
        STEPPER_BUZZ takes ~10 s), so the read timeout is at least `timeout`."""
        saved, self.timeout = self.timeout, max(self.timeout, timeout)
        try:
            self._post("/printer/gcode/script?script=" + quote(script, safe=""))
        finally:
            self.timeout = saved

    def _head_state(self, index: int) -> tuple[PrinterStatus, Slot, float | None]:
        """One read of the job state, head `index` of print_task_config and its extruder's K."""
        ext = extruder_name(index)
        raw = self.query(f"webhooks&print_stats&print_task_config&{ext}")
        if not raw.get("print_task_config"):
            raise self._unsupported("writing a head's filament (no print_task_config: not a Snapmaker U1)")
        st = parse_status(raw)
        slot = next((s for s in st.slots if s.index == index), None)
        if slot is None:
            raise PrinterRefused(f"{self.name}: print_task_config has no head {index} ({len(st.slots)} heads)",
                                 command="printer")
        pa = (raw.get(ext) or {}).get("pressure_advance")
        return st, slot, float(pa) if isinstance(pa, (int, float)) else None

    def set_head_filament(self, index: int, filament: HeadFilament, *,
                          force_official: bool = False) -> HeadWrite:
        try:
            line = filament_config_gcode(index, filament, force_official=force_official)
        except ValueError as exc:
            raise PrinterRefused(f"{self.name} head T{index + 1}: {exc}", command="printer") from exc
        st, before, _ = self._head_state(index)
        cfg = st.raw["print_task_config"]
        if st.state in (PRINTING, PAUSED, UNKNOWN):
            raise PrinterRefused(
                f"{self.name} is {st.raw_state}: writing a head's filament resets its pressure advance "
                "(FLOW_RESET_K), never done during a job",
                command="printer",
                remediation=["Load again when the printer is idle: the spool binding is already saved."],
            )
        if _at(cfg.get("filament_official"), index) and not force_official:
            raise PrinterRefused(
                f"{self.name} head T{index + 1} holds official RFID filament "
                f"({before.vendor} {before.material} {before.color}): not overwritten",
                command="printer",
                remediation=["Overwrite it on purpose with --force-official."],
            )
        self.run_gcode(line, timeout=self.timeout)
        _, after, pa = self._head_state(index)
        wrong = _differs(after, filament)
        if wrong:
            raise PrinterCommError(
                f"{self.name} head T{index + 1} after SET_PRINT_FILAMENT_CONFIG shows " + "; ".join(wrong),
                command="printer",
                remediation=[f"Check the head on the printer screen: 3d printer status {self.name}"],
            )
        return HeadWrite(index=index, extruder=extruder_name(index), before=before, after=after,
                         pressure_advance=pa)

    def set_pressure_advance(self, index: int, advance: float, *,
                             smooth_time: float | None = None) -> float:
        ext = extruder_name(index)
        line = f"SET_PRESSURE_ADVANCE EXTRUDER={ext} ADVANCE={advance:.6g}"
        if smooth_time is not None:
            line += f" SMOOTH_TIME={smooth_time:.6g}"
        self.run_gcode(line, timeout=self.timeout)
        got = (self.query(ext).get(ext) or {}).get("pressure_advance")
        if not isinstance(got, (int, float)) or abs(got - advance) > PA_TOLERANCE:
            raise PrinterCommError(
                f"{self.name} {ext} after SET_PRESSURE_ADVANCE shows pressure_advance {got!r}, sent {advance:.6g}",
                command="printer",
                remediation=[f"Set it by hand: SET_PRESSURE_ADVANCE EXTRUDER={ext} ADVANCE={advance:.6g}"],
            )
        return float(got)


__all__ = ["MoonrakerAdapter", "extruder_name", "filament_config_gcode", "parse_history", "parse_queue",
           "parse_slots", "parse_status", "parse_totals"]
