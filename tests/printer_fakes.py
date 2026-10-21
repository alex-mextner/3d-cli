"""In-process fake printers for `3d printer` tests: a fake Moonraker HTTP API and a fake
Creality stock firmware (HTTP /info + /upload, WebSocket :9999). They record every request
so tests can prove that a refused operation sent NOTHING to the printer.

Never point these tests at a real printer: only loopback fakes are used.
"""
from __future__ import annotations

import copy
import json
import shlex
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from printer_ops.ws import OP_CLOSE, OP_TEXT, accept_key, encode_frame, parse_frame

FACTORY_K = 0.02  # what the fake firmware's FLOW_RESET_K sets (U1 filament_parameters default)
U1_TASK_CONFIG: dict[str, Any] = {
    "filament_exist": [True, False, True, True],
    "filament_color_rgba": ["000000FF", "808080FF", "1B5E20FF", "FFFFFFFF"],
    "filament_type": ["PLA", "PLA", "PLA", "PLA"],
    "filament_vendor": ["Snapmaker", "Snapmaker", "Snapmaker", "Snapmaker"],
    "filament_sub_type": ["SnapSpeed", "Basic", "Basic", "Basic"],
    "filament_official": [False, False, False, False],
}


def u1_status(state: str = "standby", filename: str = "", progress: float = 0.0) -> dict[str, Any]:
    return {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": {"state": state, "filename": filename, "message": ""},
        "virtual_sdcard": {"progress": progress, "is_active": state == "printing"},
        "display_status": {"progress": progress},
        "extruder": {"temperature": 25.3, "target": 0.0, "pressure_advance": FACTORY_K},
        "extruder1": {"temperature": 24.9, "target": 0.0, "pressure_advance": FACTORY_K},
        "extruder2": {"temperature": 25.0, "target": 0.0, "pressure_advance": FACTORY_K},
        "extruder3": {"temperature": 25.1, "target": 0.0, "pressure_advance": FACTORY_K},
        "heater_bed": {"temperature": 30.0, "target": 0.0},
        "print_task_config": copy.deepcopy(U1_TASK_CONFIG),
    }


def multipart_parts(body: bytes, content_type: str) -> dict[str, bytes]:
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts: dict[str, bytes] = {}
    for chunk in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        parts[name] = data[:-2] if data.endswith(b"\r\n") else data
    return parts


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    fields: dict[str, bytes] = field(default_factory=dict)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


@dataclass
class FakeMoonraker:
    status: dict[str, Any] = field(default_factory=u1_status)
    files: dict[str, int] = field(default_factory=dict)        # name -> listed size
    contents: dict[str, bytes] = field(default_factory=dict)   # name -> bytes served on read-back
    requests: list[Recorded] = field(default_factory=list)
    upload_reply: dict[str, Any] | None = None   # override the upload JSON
    stored_size_delta: int = 0                    # pretend the printer stored N extra bytes
    scripts: list[str] = field(default_factory=list)  # G-code sent to /printer/gcode/script
    home_on_g28: bool = True                      # a G28 script marks xyz homed (False: homing fails)
    settings: dict[str, Any] = field(default_factory=lambda: {
        "stepper_x": {"position_min": -2.0, "position_max": 272.0},
        "stepper_y": {"position_max": 274.0},
    })                                            # Klipper configfile.settings
    history: list[dict[str, Any]] = field(default_factory=list)  # Moonraker job history, newest first
    queue: dict[str, Any] = field(default_factory=lambda: {"queued_jobs": [], "queue_state": "ready"})  # job_queue/status
    totals: dict[str, Any] = field(default_factory=lambda: {"job_totals": {
        "total_jobs": 0, "total_time": 0.0, "total_print_time": 0.0, "total_filament_used": 0.0,
        "longest_job": 0.0, "longest_print": 0.0}})  # history/totals
    server_info: dict[str, Any] = field(default_factory=lambda: {
        "klippy_connected": True, "klippy_state": "ready", "moonraker_version": "v0.9.3-fake"})  # /server/info
    camera: bytes | None = None                   # JPEG served at the U1 camera monitor.jpg path
    camera_hits: int = 0                          # each hit gets new bytes (a COM counter), like a live camera
    script_errors: dict[str, str] = field(default_factory=dict)  # G-code command -> Klipper error (HTTP 400)
    ignored_commands: set[str] = field(default_factory=set)      # acknowledged but without effect
    port: int = 0

    def writes(self) -> list[Recorded]:
        return [r for r in self.requests if r.method == "POST"]

    def run_script(self, script: str) -> str | None:
        """What the U1 firmware does with the head-filament and pressure-advance commands
        (u1-klipper print_task_config.py:585-711, flow_calibrator.py FLOW_RESET_K); the Klipper
        error for a refused line, None when it ran."""
        first = script.split(None, 1)
        cmd = first[0].upper() if first else ""
        if cmd in self.script_errors:
            return self.script_errors[cmd]
        if cmd in self.ignored_commands or cmd not in ("SET_PRINT_FILAMENT_CONFIG", "SET_PRESSURE_ADVANCE"):
            return None
        params = {k.upper(): v for k, _, v in (w.partition("=") for w in shlex.split(script)[1:])}
        if cmd == "SET_PRINT_FILAMENT_CONFIG":
            i = int(params["CONFIG_EXTRUDER"])
            cfg = self.status["print_task_config"]
            if cfg.get("filament_official", [False] * 4)[i] and params.get("FORCE") != "1":
                return "[print_task_config] filament_config, official filament, not configurable!"
            if "FILAMENT_TYPE" in params:
                cfg["filament_vendor"][i] = params["VENDOR"]
                cfg["filament_type"][i] = params["FILAMENT_TYPE"]
                cfg["filament_sub_type"][i] = params["FILAMENT_SUBTYPE"]
            if "FILAMENT_COLOR_RGBA" in params:
                cfg["filament_color_rgba"][i] = params["FILAMENT_COLOR_RGBA"]
            cfg.setdefault("filament_official", [False] * 4)[i] = False
            self.status["extruder" if i == 0 else f"extruder{i}"]["pressure_advance"] = FACTORY_K
        elif cmd == "SET_PRESSURE_ADVANCE":
            self.status[params["EXTRUDER"]]["pressure_advance"] = float(params["ADVANCE"])
        return None

    def put(self, name: str, data: bytes) -> None:
        """A file already stored on the printer (listed size = its length)."""
        self.files[name] = len(data)
        self.contents[name] = data

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _reply(self, obj: Any, code: int = 200) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                u = urlsplit(self.path)
                fake.requests.append(Recorded("GET", u.path, parse_qs(u.query)))
                if u.path == "/printer/objects/query" and "configfile" in parse_qs(u.query, keep_blank_values=True):
                    self._reply({"result": {"eventtime": 1.0, "status": {"configfile": {"settings": fake.settings}}}})
                elif u.path == "/printer/objects/query":
                    self._reply({"result": {"eventtime": 1.0, "status": fake.status}})
                elif u.path == "/server/history/list":
                    q = parse_qs(u.query)
                    start, limit = int(q.get("start", ["0"])[0]), int(q.get("limit", ["50"])[0])
                    self._reply({"result": {"count": len(fake.history),
                                            "jobs": fake.history[start:start + limit]}})
                elif u.path == "/server/job_queue/status":
                    self._reply({"result": fake.queue})
                elif u.path == "/server/history/totals":
                    self._reply({"result": fake.totals})
                elif u.path == "/server/info":
                    self._reply({"result": fake.server_info})
                elif u.path == "/server/files/list":
                    self._reply({"result": [{"path": n, "size": s, "modified": 1.0} for n, s in fake.files.items()]})
                elif u.path.startswith("/server/files/gcodes/") and unquote(u.path[21:]) in fake.contents:
                    data = fake.contents[unquote(u.path[21:])]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif u.path == "/server/files/camera/monitor.jpg" and fake.camera is not None:
                    fake.camera_hits += 1
                    com = b"\xff\xfe\x00\x0a" + fake.camera_hits.to_bytes(8, "big")
                    data = fake.camera[:2] + com + fake.camera[2:]
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._reply({"error": {"code": 404, "message": "not found"}}, 404)

            def do_POST(self) -> None:
                u = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = Recorded("POST", u.path, parse_qs(u.query))
                fake.requests.append(rec)
                if u.path == "/server/files/upload":
                    rec.fields = multipart_parts(body, self.headers["Content-Type"])
                    filename = self._filename(body)
                    fake.files[filename] = len(rec.fields["file"]) + fake.stored_size_delta
                    fake.contents[filename] = rec.fields["file"]
                    self._reply(fake.upload_reply or {"result": {
                        "item": {"path": filename, "root": "gcodes"},
                        "print_started": False, "print_queued": False, "action": "create_file"}}, 201)
                elif u.path == "/printer/print/start":
                    fake.status["print_stats"].update(state="printing", filename=rec.query["filename"][0])
                    self._reply({"result": "ok"})
                elif u.path == "/printer/print/cancel":
                    fake.status["print_stats"].update(state="cancelled")
                    self._reply({"result": "ok"})
                elif u.path == "/printer/print/pause":
                    fake.status["print_stats"].update(state="paused")
                    self._reply({"result": "ok"})
                elif u.path == "/printer/gcode/script":
                    fake.scripts.append(rec.query["script"][0])
                    if fake.scripts[-1].strip().upper() == "G28" and fake.home_on_g28:
                        fake.status.setdefault("toolhead", {})["homed_axes"] = "xyz"  # Klipper homes all
                    error = fake.run_script(fake.scripts[-1])
                    if error is not None:
                        self._reply({"error": {"code": 400, "message": error}}, 400)
                    else:
                        self._reply({"result": "ok"})
                else:
                    self._reply({"error": {"code": 404}}, 404)

            @staticmethod
            def _filename(body: bytes) -> str:
                return body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()

        return H

    @contextmanager
    def serve(self) -> Iterator[FakeMoonraker]:
        srv = _Server(("127.0.0.1", 0), self.handler())
        self.port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()


def k1_state(state: int = 0, device_state: int = 0, file: str = "") -> dict[str, Any]:
    return {
        "hostname": "K1SE-85B6", "model": "K1 SE", "state": state, "deviceState": device_state,
        "printFileName": f"/usr/data/printer_data/gcodes/{file}" if file else "",
        "printProgress": 37 if state == 1 else 0, "nozzleTemp": "210.120000", "targetNozzleTemp": 210,
        "bedTemp0": "60.010000", "targetBedTemp0": 60, "err": {"errcode": 0, "key": 0, "value": ""},
    }


@dataclass
class FakeCreality:
    state: dict[str, Any] = field(default_factory=k1_state)
    files: dict[str, int] = field(default_factory=dict)
    http_requests: list[Recorded] = field(default_factory=list)
    ws_received: list[str] = field(default_factory=list)
    stored_size_delta: int = 0
    index_html: str = "<html><head><title>Creality K1 SE</title></head><body>web UI</body></html>"  # GET /
    start_reply: str = "ack"  # "ack": push the running state; "silent": say nothing; "close": hang up
    http_port: int = 0
    ws_port: int = 0

    def ws_commands(self) -> list[dict[str, Any]]:
        out = []
        for m in self.ws_received:
            try:
                obj = json.loads(m)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def writes(self) -> list[str]:
        """Everything that would change the printer: uploads + ws `set` commands."""
        ups = [f"upload {r.path}" for r in self.http_requests if r.method == "POST"]
        sets = [json.dumps(c) for c in self.ws_commands() if c.get("method") == "set"]
        return ups + sets

    def _http_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _reply(self, obj: Any) -> None:
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                fake.http_requests.append(Recorded("GET", self.path, {}))
                if self.path == "/":
                    data = fake.index_html.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._reply({"model": "K1 SE", "version": "1.0.0", "hostname": "K1SE-85B6"})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                rec = Recorded("POST", self.path, {}, multipart_parts(body, self.headers["Content-Type"]))
                fake.http_requests.append(rec)
                name = unquote(self.path.split("/upload/", 1)[1])
                fake.files[name] = len(rec.fields["file"]) + fake.stored_size_delta
                self._reply({"code": 200, "message": "OK"})

        return H

    def _ws_session(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept_key(key.decode()).encode() + b"\r\n\r\n")

        def push(obj: Any) -> None:
            text = obj if isinstance(obj, str) else json.dumps(obj)
            conn.sendall(encode_frame(OP_TEXT, text.encode(), mask=False))

        push(self.state)
        push({"ModeCode": "heart_beat", "msg": "2026-10-13 12:00:00"})
        conn.settimeout(5)
        while True:
            frame = parse_frame(buf)
            if frame is None:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                continue
            _, op, data, used = frame
            buf = buf[used:]
            if op == OP_CLOSE:
                return
            msg = data.decode()
            self.ws_received.append(msg)
            try:
                cmd = json.loads(msg)
            except ValueError:
                continue
            params = cmd.get("params", {}) if isinstance(cmd, dict) else {}
            if params.get("reqGcodeFile"):
                listing = [{"name": n, "path": f"/usr/data/printer_data/gcodes/{n}", "file_size": s}
                           for n, s in self.files.items()]
                push({"retGcodeFileInfo2": listing[:-1]})  # first snapshot lags behind, like the K1
                push({"retGcodeFileInfo2": listing})
            elif "opGcodeFile" in params:
                path = params["opGcodeFile"].split(":", 1)[1]
                if self.start_reply == "close":
                    return  # hang up without a word (the start may or may not have happened)
                if self.start_reply == "ack":
                    self.state.update(state=1, deviceState=1, printFileName=path)
                    push({"state": 1, "deviceState": 1, "printFileName": path})
            elif params.get("stop") == 1:
                self.state.update(state=4, deviceState=0)
                push({"state": 4, "deviceState": 0})

    @contextmanager
    def serve(self) -> Iterator[FakeCreality]:
        srv = _Server(("127.0.0.1", 0), self._http_handler())
        self.http_port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        lsock = socket.socket()
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(8)
        self.ws_port = lsock.getsockname()[1]
        stop = threading.Event()

        def accept_loop() -> None:
            lsock.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = lsock.accept()
                except OSError:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        try:
            yield self
        finally:
            stop.set()
            srv.shutdown()
            srv.server_close()
            lsock.close()

    def _serve_conn(self, conn: socket.socket) -> None:
        with conn:
            self._ws_session(conn)


HISTORY_JOBS: list[dict[str, Any]] = [  # Moonraker /server/history/list jobs, newest first
    {"job_id": "2", "filename": "benchy.gcode", "status": "completed", "start_time": 1_700_000_000.0,
     "print_duration": 8040.0, "filament_used": 12400.0},
    {"job_id": "1", "filename": "cube.gcode", "status": "cancelled", "start_time": 1_699_900_000.0,
     "print_duration": 610.0, "filament_used": 800.0},
    {"job_id": "0", "filename": "old.gcode", "status": "completed", "start_time": 1_699_800_000.0,
     "print_duration": 60.0, "filament_used": 100.0},
]
HISTORY_TOTALS: dict[str, Any] = {"job_totals": {
    "total_jobs": 127, "total_time": 900000.0, "total_print_time": 770000.0, "total_filament_used": 412300.0}}
JOB_QUEUE: dict[str, Any] = {"queue_state": "ready", "queued_jobs": [
    {"filename": "next_part.gcode", "job_id": "A", "time_in_queue": 185.0}]}

FLUIDD_HTML = "<!doctype html><html><head><title>Fluidd</title></head><body><div id=app></div></body></html>"
MAINSAIL_HTML = "<!DOCTYPE html><html><head><title>Mainsail</title></head><body><div id=app></div></body></html>"


@dataclass
class FakeWebServer:
    """A printer web UI (Fluidd, Mainsail, ...): GET / answers `html` with HTTP `status`."""

    html: str = FLUIDD_HTML
    status: int = 200
    requests: list[str] = field(default_factory=list)
    port: int = 0

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.requests.append(self.path)
                data = fake.html.encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H

    @contextmanager
    def serve(self) -> Iterator[FakeWebServer]:
        srv = _Server(("127.0.0.1", 0), self.handler())
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()


def free_port() -> int:
    """A loopback port nothing listens on (connection refused), for the unreachable paths."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_devices(config_home: Path, moon: FakeMoonraker | None = None, k1: FakeCreality | None = None) -> Path:
    """printer-devices.yaml pointing `u1`/`k1se` at the fakes (Creality ws port via ws_port key)."""
    d = config_home / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    if moon is not None:
        lines += ["u1:", "  adapter: moonraker", "  host: 127.0.0.1", f"  port: {moon.port}", "  model: Snapmaker U1",
                  "  trusted_macros: [T0, T1, T2, T3]"]  # the U1's toolchange macros, as in a real config
    if k1 is not None:
        lines += ["k1se:", "  adapter: creality-stock", "  host: 127.0.0.1", f"  port: {k1.http_port}",
                  f"  ws_port: {k1.ws_port}", "  model: Creality K1 SE",
                  "  axis_limits: {x: [-5, 229], y: [-0.5, 227]}"]
    path = d / "printer-devices.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


U1_OBJECTS = ["webhooks", "print_stats", "virtual_sdcard", "display_status", "pause_resume", "idle_timeout",
              "toolhead", "exception_manager", "extruder", "extruder1", "heater_bed",
              "filament_motion_sensor e0_filament", "gcode_macro PAUSE"]


def u1_ws_status(state: str = "printing", **over: Any) -> dict[str, Any]:
    """Full subscribe-result status of a U1 in `state` (fields as printer.objects.subscribe returns)."""
    status: dict[str, Any] = {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": {"state": state, "filename": "part-0123456789abcdef.gcode", "message": "",
                        "info": {"total_layer": 1193, "current_layer": 828}, "exception": {}},
        "pause_resume": {"is_paused": state == "paused"},
        "virtual_sdcard": {"progress": 0.62, "file_position": 577487, "is_active": state == "printing"},
        "display_status": {"progress": 0.62, "message": None},
        "idle_timeout": {"state": "Printing"},
        "toolhead": {"homed_axes": "xyz"},
        "exception_manager": {"exceptions": []},
        "extruder": {"temperature": 220.0, "target": 220.0},
        "extruder1": {"temperature": 33.0, "target": 0.0},
        "heater_bed": {"temperature": 65.0, "target": 65.0},
        "filament_motion_sensor e0_filament": {"filament_detected": True, "enabled": True},
    }
    for obj, fields in over.items():
        status[obj.replace("__", " ")].update(fields)
    return status


@dataclass
class FakeMoonrakerWS:
    """Moonraker's /websocket: answers server.info / printer.objects.list / .subscribe and
    server.files.metadata (from `metadata`, else "not found"), then pushes `pushes` (JSON-RPC
    notifications) one by one. Records every request method so tests can prove the watcher
    only ever asks read-only questions."""

    status: dict[str, Any] = field(default_factory=u1_ws_status)
    objects: list[str] = field(default_factory=lambda: list(U1_OBJECTS))
    pushes: list[dict[str, Any]] = field(default_factory=list)
    klippy_state: str = "ready"
    push_gap_s: float = 0.05
    methods: list[str] = field(default_factory=list)
    subscribed: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)  # filename -> metadata result
    metadata_silent: bool = False  # never answer server.files.metadata (a hung endpoint)
    port: int = 0

    def _session(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept_key(key.decode()).encode() + b"\r\n\r\n")

        def send(obj: Any) -> None:
            conn.sendall(encode_frame(OP_TEXT, json.dumps(obj).encode(), mask=False))

        conn.settimeout(0.05)
        pending = list(self.pushes)
        subscribed = False  # pushes start after subscribe (or after server.info when klippy is not ready)
        next_push = 0.0
        while True:
            frame = parse_frame(buf)
            if frame is None:
                if subscribed and pending and time.monotonic() >= next_push:
                    send(pending.pop(0))
                    next_push = time.monotonic() + self.push_gap_s
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                continue
            _, op, data, used = frame
            buf = buf[used:]
            if op == OP_CLOSE:
                return
            req = json.loads(data.decode())
            method = req.get("method")
            self.methods.append(method)
            if method == "server.info":
                send({"jsonrpc": "2.0", "id": req["id"], "result": {"klippy_state": self.klippy_state}})
                subscribed = subscribed or self.klippy_state != "ready"
            elif method == "printer.objects.list":
                send({"jsonrpc": "2.0", "id": req["id"], "result": {"objects": self.objects}})
            elif method == "printer.objects.subscribe":
                self.subscribed.append(req["params"]["objects"])
                send({"jsonrpc": "2.0", "id": req["id"], "result": {"eventtime": 1.0, "status": self.status}})
                subscribed = True
            elif method == "server.files.metadata" and self.metadata_silent:
                pass
            elif method == "server.files.metadata" and req["params"]["filename"] in self.metadata:
                send({"jsonrpc": "2.0", "id": req["id"], "result": self.metadata[req["params"]["filename"]]})
            else:
                send({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "not found"}})

    @contextmanager
    def serve(self) -> Iterator[FakeMoonrakerWS]:
        lsock = socket.socket()
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(8)
        self.port = lsock.getsockname()[1]
        stop = threading.Event()

        def accept_loop() -> None:
            lsock.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = lsock.accept()
                except OSError:
                    continue
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        try:
            yield self
        finally:
            stop.set()
            lsock.close()

    def _serve_conn(self, conn: socket.socket) -> None:
        with conn:
            self._session(conn)
