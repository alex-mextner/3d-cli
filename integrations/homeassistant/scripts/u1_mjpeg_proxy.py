#!/usr/bin/env python3
"""Expose the Snapmaker U1 monitor JPEG as an MJPEG stream for go2rtc."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SOURCE_URL = os.environ.get(
    "U1_CAMERA_URL",
    "http://192.168.0.63:7125/server/files/camera/monitor.jpg",
)
PORT = int(os.environ.get("U1_CAMERA_PORT", "8090"))
FPS = max(0.2, float(os.environ.get("U1_CAMERA_FPS", "1.0")))
TIMEOUT = float(os.environ.get("U1_CAMERA_TIMEOUT", "5"))
BOUNDARY = "u1camera"
START_MONITOR = os.environ.get("U1_CAMERA_START_MONITOR", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
START_MONITOR_URL = os.environ.get(
    "U1_CAMERA_START_MONITOR_URL",
    "http://192.168.0.63:7125/server/mqtt/publish",
)
START_MONITOR_INTERVAL = max(
    60.0,
    float(os.environ.get("U1_CAMERA_START_MONITOR_INTERVAL", "240")),
)
START_MONITOR_CLIENT_ID = os.environ.get(
    "U1_CAMERA_START_MONITOR_CLIENT_ID",
    "686f6d65617373697374616e742d676f327274632d75310000000000000000",
)
START_MONITOR_DOMAIN = os.environ.get("U1_CAMERA_START_MONITOR_DOMAIN", "wan")


class FrameCache:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: bytes | None = None
        self._updated_at = 0.0
        self._error = ""

    def start(self) -> None:
        thread = threading.Thread(target=self._poll, name="u1-frame-poller", daemon=True)
        thread.start()

    def get(self) -> tuple[bytes | None, float, str]:
        with self._condition:
            return self._frame, self._updated_at, self._error

    def wait_for_frame(self, last_seen: float, timeout: float) -> tuple[bytes | None, float, str]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._updated_at > last_seen or bool(self._error),
                timeout=timeout,
            )
            return self._frame, self._updated_at, self._error

    def _poll(self) -> None:
        delay = 1.0 / FPS
        while True:
            started = time.monotonic()
            try:
                frame = fetch_frame()
                with self._condition:
                    self._frame = frame
                    self._updated_at = time.time()
                    self._error = ""
                    self._condition.notify_all()
            except Exception as err:  # noqa: BLE001
                with self._condition:
                    self._error = str(err)
                    self._condition.notify_all()

            elapsed = time.monotonic() - started
            time.sleep(max(0.05, delay - elapsed))


def source_url_with_cache_bust() -> str:
    parsed = urllib.parse.urlsplit(SOURCE_URL)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("cacheBust", str(int(time.time() * 1000))))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def fetch_frame() -> bytes:
    request = urllib.request.Request(
        source_url_with_cache_bust(),
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "")
        if "image/jpeg" not in content_type:
            raise urllib.error.URLError(f"unexpected content type: {content_type}")
        return response.read()


cache = FrameCache()


class CameraMonitorKeepalive:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._error = ""

    def start(self) -> None:
        if not START_MONITOR:
            return
        thread = threading.Thread(target=self._run, name="u1-camera-keepalive", daemon=True)
        thread.start()

    def get(self) -> tuple[float, str]:
        with self._lock:
            return self._started_at, self._error

    def _run(self) -> None:
        while True:
            try:
                start_monitor()
                with self._lock:
                    self._started_at = time.time()
                    self._error = ""
                print("U1 camera monitor keepalive sent", flush=True)
            except Exception as err:  # noqa: BLE001
                with self._lock:
                    self._error = str(err)
                print(f"U1 camera monitor keepalive failed: {err}", flush=True)
            time.sleep(START_MONITOR_INTERVAL)


def start_monitor() -> None:
    request_id = int(time.time() * 1000)
    camera_request = {
        "jsonrpc": "2.0",
        "method": "camera.start_monitor",
        "params": {
            "domain": START_MONITOR_DOMAIN,
            "interval": 0,
            "expect_pw": True,
            "clientid": START_MONITOR_CLIENT_ID,
        },
        "id": request_id,
    }
    publish_request = {
        "topic": "camera/request",
        "payload": json.dumps(camera_request, separators=(",", ":")),
        "qos": 0,
        "retain": False,
    }
    body = json.dumps(publish_request, separators=(",", ":")).encode()
    request = urllib.request.Request(
        START_MONITOR_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if response.status != HTTPStatus.OK:
            raise urllib.error.URLError(f"unexpected start_monitor status: {response.status}")


keepalive = CameraMonitorKeepalive()


class Handler(BaseHTTPRequestHandler):
    server_version = "U1MJPEG/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._health()
        elif self.path.startswith("/u1.jpg"):
            self._snapshot()
        elif self.path.startswith("/u1.mjpeg"):
            self._mjpeg()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _health(self) -> None:
        frame, updated_at, error = cache.get()
        monitor_started_at, monitor_error = keepalive.get()
        stale_for = time.time() - updated_at if updated_at else -1
        status = HTTPStatus.OK if frame and stale_for < 30 else HTTPStatus.SERVICE_UNAVAILABLE
        monitor_age = time.time() - monitor_started_at if monitor_started_at else -1
        body = (
            f"frame={bool(frame)} stale_for={stale_for:.1f} error={error} "
            f"monitor_keepalive={START_MONITOR} monitor_age={monitor_age:.1f} "
            f"monitor_error={monitor_error}\n"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self) -> None:
        frame, _, error = cache.get()
        if not frame:
            self.send_error(HTTPStatus.BAD_GATEWAY, error or "no frame")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _mjpeg(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        last_seen = 0.0
        while True:
            frame, updated_at, _ = cache.wait_for_frame(last_seen, timeout=2.0)
            if not frame:
                continue
            last_seen = updated_at
            try:
                self.wfile.write(
                    (
                        f"--{BOUNDARY}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode()
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


def main() -> None:
    keepalive.start()
    cache.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving U1 MJPEG proxy on :{PORT}, source={SOURCE_URL}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
