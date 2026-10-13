"""creality.py — Creality stock-firmware adapter (K1 / K1 SE family, no Moonraker).

PROTOCOL — the same one OrcaSlicer's "Creality Print" host (src/slic3r/Utils/CrealityPrint.cpp)
and the printer's own web UI use; issue https://github.com/alex-mextner/3d-cli/issues/93:
  probe   GET  http://host/info -> {"model": "K1 SE", "version": ...}       (CrealityPrint.cpp:94-134)
  upload  POST http://host/upload/<name>, multipart with ONLY the `file` field (see below)
  status  ws://host:9999 pushes JSON state deltas; a "heart_beat" text must be answered "ok"
  files   {"method":"get","params":{"reqGcodeFile":1}} -> several retGcodeFileInfo2 snapshots
  start   {"method":"set","params":{"opGcodeFile":"printprt:/usr/data/printer_data/gcodes/<name>"}}
  stop    {"method":"set","params":{"stop":1}}                                 (web UI app.*.js)

PAST BUG (issue https://github.com/alex-mextner/3d-cli/issues/99): sending the extra multipart
  field `path=""` made the K1 SE store the file 165-168 bytes longer (multipart trailer saved
  into it) in 4 of 4 uploads; with only `file` it was byte-identical in 5 of 5. Files of about
  170 bytes or less keep the trailer anyway — so the stored size is ALWAYS compared.

STATE CODES (observed on a K1 SE, 2026-10): state 0 idle, 1 printing, 2 complete;
  deviceState 0 idle, 1 busy. Other codes are unverified: with deviceState 0 they are reported
  as idle with the raw code in the message (the proven start rule of garage-band
  gen/k1_print.py: refuse while deviceState != 0 or state == 1).

UPLOAD WHILE BUSY: refused — overwriting the file being printed is unsafe and the firmware
  does not check it.
"""
from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from printer_ops.base import (
    BUSY,
    COMPLETE,
    IDLE,
    PRINTING,
    UNKNOWN,
    Adapter,
    PrinterCommError,
    PrinterStatus,
    RemoteFile,
    UploadResult,
    http_json,
    http_upload,
)
from printer_ops.ws import WebSocket, WebSocketClosed, WebSocketError

GCODE_DIR = "/usr/data/printer_data/gcodes"


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def merge_messages(msgs: list[str]) -> dict[str, Any]:
    """Pushed JSON deltas -> one state dict (later keys win); non-JSON frames are ignored."""
    state: dict[str, Any] = {}
    for m in msgs:
        try:
            obj = json.loads(m)
        except ValueError:
            continue
        if isinstance(obj, dict):
            state.update(obj)
    return state


def parse_status(s: dict[str, Any], info: dict[str, Any] | None = None) -> PrinterStatus:
    st, ds = _int(s.get("state")), _int(s.get("deviceState"))
    message = None
    if not s:
        state = UNKNOWN
        message = "the printer pushed no state on ws:9999 within the listen window"
    elif st == 1:
        state = PRINTING
    elif ds not in (0, None):
        state = BUSY
    elif st == 0:
        state = IDLE
    elif st == 2:
        state = COMPLETE
    elif ds == 0:
        state = IDLE
        message = f"state code {st} is not mapped; deviceState 0 (device idle)"
    else:
        state = UNKNOWN
    err = s.get("err")
    if isinstance(err, dict) and _int(err.get("errcode")):
        message = f"err {err.get('errcode')} {err.get('key', '')} {err.get('value', '')}".strip()
    progress = _num(s.get("printProgress"))
    temps = {
        "nozzle": (_num(s.get("nozzleTemp")), _num(s.get("targetNozzleTemp"))),
        "bed": (_num(s.get("bedTemp0")), _num(s.get("targetBedTemp0"))),
    }
    return PrinterStatus(
        state=state,
        raw_state=f"state={s.get('state')} deviceState={s.get('deviceState')}",
        filename=os.path.basename(str(s["printFileName"])) if s.get("printFileName") else None,
        progress=progress / 100.0 if progress is not None else None,
        temps={k: v for k, v in temps.items() if v != (None, None)},
        message=message,
        model=(info or {}).get("model"),
        raw=s,
    )


class CrealityStockAdapter(Adapter):
    kind = "creality-stock"
    upload_while_busy = False

    def __init__(self, name: str, host: str, port: int = 80, *, ws_port: int = 9999,
                 listen_s: float = 3.0, files_listen_s: float = 5.0, confirm_s: float = 15.0,
                 timeout: float = 10.0, upload_timeout: float = 600.0) -> None:
        super().__init__(name, host, port)
        self.ws_port = ws_port
        self.listen_s = listen_s
        self.files_listen_s = files_listen_s
        self.confirm_s = confirm_s
        self.timeout = timeout
        self.upload_timeout = upload_timeout

    # ---- channel helpers ------------------------------------------------------------------
    def _connect(self) -> WebSocket:
        try:
            return WebSocket.connect(self.host, self.ws_port, timeout=self.timeout)
        except OSError as exc:
            raise self._comm_error(f"ws://{self.host}:{self.ws_port}", exc) from exc

    def _collect(self, ws: WebSocket, secs: float, until: Callable[[str], bool] | None = None) -> list[str]:
        """Read pushed messages for `secs`, answering heart_beat; stop early when until(msg)."""
        msgs: list[str] = []
        deadline = time.monotonic() + secs
        while (left := deadline - time.monotonic()) > 0:
            try:
                m = ws.recv(left)
            except WebSocketClosed:
                break
            if m is None:
                break
            if "heart_beat" in m:
                ws.send_text("ok")
                continue
            msgs.append(m)
            if until and until(m):
                break
        return msgs

    def _send(self, payload: dict[str, Any], secs: float, until: Callable[[str], bool] | None = None) -> list[str]:
        ws = self._connect()
        try:
            ws.send_text(json.dumps(payload))
            return self._collect(ws, secs, until)
        except WebSocketError as exc:
            raise self._comm_error("ws", exc) from exc
        finally:
            ws.close()

    def info(self) -> dict[str, Any]:
        try:
            body = http_json("GET", f"http://{self.host}:{self.port}/info", timeout=self.timeout)
        except (OSError, ValueError) as exc:
            raise self._comm_error("GET /info", exc) from exc
        return body if isinstance(body, dict) else {}

    # ---- operations -----------------------------------------------------------------------
    def status(self) -> PrinterStatus:
        info = self.info()
        ws = self._connect()
        try:
            msgs = self._collect(ws, self.listen_s)
        except WebSocketError as exc:
            raise self._comm_error("ws", exc) from exc
        finally:
            ws.close()
        return parse_status(merge_messages(msgs), info)

    def list_files(self, expect: tuple[str, int] | None = None) -> dict[str, RemoteFile]:
        """Merge every retGcodeFileInfo2 snapshot seen in the window: right after an upload
        some snapshots still lack the new file. `expect=(name, size)` stops early on a match."""
        files: dict[str, RemoteFile] = {}

        def absorb(m: str) -> bool:
            if "retGcodeFileInfo2" not in m:
                return False
            try:
                entries = json.loads(m)["retGcodeFileInfo2"]
            except (ValueError, KeyError, TypeError):
                return False
            for e in entries if isinstance(entries, list) else []:
                if isinstance(e, dict) and e.get("name"):
                    files[e["name"]] = RemoteFile(e["name"], _int(e.get("file_size")))
            return expect is not None and (got := files.get(expect[0])) is not None and got.size == expect[1]

        self._send({"method": "get", "params": {"reqGcodeFile": 1}}, self.files_listen_s, absorb)
        return files

    def upload(self, local: Path, remote_name: str) -> UploadResult:
        size = local.stat().st_size
        url = f"http://{self.host}:{self.port}/upload/{quote(remote_name)}"
        try:
            status, data = http_upload(url, local, remote_name, {}, timeout=self.upload_timeout)
        except (socket.timeout, TimeoutError) as exc:
            status, data = -1, repr(exc).encode()
        except OSError as exc:
            raise self._comm_error("upload", exc) from exc
        if status not in (-1, 200):
            raise self._comm_error("upload", f"HTTP {status}: {data[:300]!r}")
        if status == 200:
            try:
                code = json.loads(data).get("code")
            except (ValueError, AttributeError):
                code = None
            if code not in (200, None):
                raise self._comm_error("upload", f"printer replied {data[:300]!r}")
        remote = self.list_files(expect=(remote_name, size)).get(remote_name)
        if remote is None:
            outcome = "timed out and the file is not listed — outcome UNKNOWN" if status == -1 else \
                "the file is not in the printer's list after upload"
            raise PrinterCommError(
                f"{self.name}: upload of {remote_name}: {outcome}",
                command="printer",
                remediation=[f"Re-read before retrying: 3d printer status {self.name}"],
            )
        if remote.size != size:
            raise self._comm_error(
                "upload", f"size mismatch: local {size} B, printer {remote.size} B "
                "(stock K1 firmware keeps a multipart tail on very small files)")
        note = "upload response timed out; size verified from the file list" if status == -1 else ""
        return UploadResult(remote_name, size, verified=True, note=note)

    def start(self, remote_name: str) -> None:
        cmd = {"method": "set", "params": {"opGcodeFile": f"printprt:{GCODE_DIR}/{remote_name}"}}
        self._send(cmd, self.confirm_s, lambda m: remote_name in m and "printFileName" in m)

    def cancel(self) -> None:
        self._send({"method": "set", "params": {"stop": 1}}, 2.0)
