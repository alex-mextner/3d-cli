"""topcam.node — the camera node: an HTTP service around one UVC camera (the top camera).

WHAT: serves full-resolution uncompressed stills on demand with the camera locked to manual
  focus/exposure/gain/white balance, a low-resolution MJPEG preview through ustreamer, the
  camera's controls, an optional LED ring on a GPIO pin, and registers itself with 3d-api
  (`PUT /v1/camera-nodes/{name}`) every `api.interval_s` seconds.
WHY: a Raspberry Pi Zero W above the bed turns the Logitech C920 into a network camera that
  `3d calibrate photo --camera topcam` can drive; the same service runs on the PC
  (`3d camera serve`) so the HTTP path is testable without a Pi.

HTTP API (JSON unless noted; errors are {"error": "..."} with 400/404/409/500/503):
  GET  /health                 {"ok", "name", "version", "device", "card", "busy", "stream", "led"}
  GET  /controls               {"controls": [...], "formats": {"YUYV": [[w, h], ...], ...}}
  POST /controls               body {"focus": 40, "exposure": 300, "exposure_auto": 1, ...} -> read-back
  GET  /still?res=2304x1536&fmt=yuyv|grey|pgm&frames=N&skip=K&focus=F&exposure=E&gain=G&wb=K
             &ctrl=NAME=V[&ctrl=...]&led=on|off&settle_ms=MS
                               body: N frames back to back (yuyv: W*H*2 bytes each; grey: W*H;
                               pgm: N binary PGM images); headers X-Width, X-Height, X-Format,
                               X-Frames, X-Dropped, X-Controls (JSON read-back after capture)
  GET  /stream, /snapshot      307 to ustreamer (`stream.port`); 404 when the preview is off
  POST /led                    body {"on": true|false} -> {"led": bool}
The preview stream and a still cannot share the camera: a still stops ustreamer, captures,
and starts it again; requests are serialized (409 when a still is already running and
`wait=0` is given).

CONFIG (`--config FILE`, JSON; every key optional):
  {"name": "topcam", "port": 8080, "device": "auto" | "/dev/v4l/by-id/...",
   "still": {"res": "2304x1536", "skip": 3, "max_frames": 8,
             "lock": {"focus": 30, "exposure": 300, "gain": 0, "wb": 4500}},
   "stream": {"enabled": true, "port": 8081, "res": "640x480", "fps": 5, "binary": "ustreamer"},
   "led": {"gpio": 17, "active_high": true, "settle_ms": 300},
   "api": {"url": "http://192.168.0.39:8765", "token_file": "/etc/topcam/token", "interval_s": 60}}

INVARIANTS: stdlib only, no imports from the rest of `lib/` except `topcam.v4l2` (the SD
  image ships the `topcam` package alone). A still never returns a frame taken before the
  requested lock was applied: the lock is set while streaming is off, then `skip` frames
  are dropped.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import re
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

if __package__ in (None, ""):  # `python3 node.py` from inside the package directory
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topcam import v4l2
from topcam.focus import (
    DEFAULT_ROI,
    SWEEP_RES,
    FocusError,
    FocusMode,
    parse_focus,
    parse_roi,
    sweep,
)

VERSION = "1.0.0"
# What the SD image installs as the `topcam` package on the Pi (lib/topcam/<file>).
NODE_FILES = ("__init__.py", "v4l2.py", "focus.py", "node.py")
# The camera-node name rule 3d-api enforces (PUT /v1/camera-nodes/{name}).
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
LOG = logging.getLogger("topcam")
FORMATS = ("yuyv", "grey", "pgm")
MAX_SETTLE_MS = 5000  # LED settle before a still; bounds how long one request holds the camera
MAX_SKIP = 120  # frames dropped before a still; bounds how long one request holds the camera
LOCK_KEYS = ("focus", "exposure", "gain", "wb")


class NodeError(Exception):
    """A request the node refuses; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def parse_res(text: str) -> tuple[int, int]:
    w, sep, h = text.lower().partition("x")
    if not sep or not w.isdigit() or not h.isdigit() or int(w) < 1 or int(h) < 1:
        raise NodeError(f"res must be WIDTHxHEIGHT, e.g. 2304x1536 (got {text!r})")
    return int(w), int(h)


@dataclass
class Config:
    name: str = "topcam"
    port: int = 8080
    device: str = "auto"
    still_res: str = "2304x1536"
    still_skip: int = 3
    max_frames: int = 8
    lock: dict[str, int] = field(default_factory=dict)
    stream_enabled: bool = True
    stream_port: int = 8081
    stream_res: str = "640x480"
    stream_fps: int = 5
    stream_binary: str = "ustreamer"
    led_gpio: int | None = None
    led_active_high: bool = True
    led_settle_ms: int = 300
    api_url: str | None = None
    api_token_file: str | None = None
    api_interval_s: int = 60

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Config:
        still = data.get("still", {})
        stream = data.get("stream", {})
        led = data.get("led", {})
        api = data.get("api", {})
        cfg = cls(
            name=str(data.get("name", cls.name)),
            port=int(data.get("port", cls.port)),
            device=str(data.get("device", cls.device)),
            still_res=str(still.get("res", cls.still_res)),
            still_skip=int(still.get("skip", cls.still_skip)),
            max_frames=int(still.get("max_frames", cls.max_frames)),
            lock={str(k): v if k == "focus" else int(v) for k, v in still.get("lock", {}).items()},
            stream_enabled=bool(stream.get("enabled", cls.stream_enabled)),
            stream_port=int(stream.get("port", cls.stream_port)),
            stream_res=str(stream.get("res", cls.stream_res)),
            stream_fps=int(stream.get("fps", cls.stream_fps)),
            stream_binary=str(stream.get("binary", cls.stream_binary)),
            led_gpio=None if led.get("gpio") is None else int(led["gpio"]),
            led_active_high=bool(led.get("active_high", True)),
            led_settle_ms=int(led.get("settle_ms", cls.led_settle_ms)),
            api_url=api.get("url") or None,
            api_token_file=api.get("token_file") or None,
            api_interval_s=int(api.get("interval_s", cls.api_interval_s)),
        )
        parse_res(cfg.still_res)
        parse_res(cfg.stream_res)
        unknown = set(cfg.lock) - set(LOCK_KEYS)
        if unknown:
            raise NodeError(
                f"still.lock: unknown key(s) {sorted(unknown)}; accepted: {LOCK_KEYS}"
            )
        if not NAME_RE.fullmatch(cfg.name):
            raise NodeError(
                f"name {cfg.name!r}: 1-64 of letters, digits, _ . - starting with a letter "
                "or digit (3d-api refuses other names)"
            )
        try:
            parse_focus(cfg.lock.get("focus"))
        except FocusError as exc:
            raise NodeError(f"still.lock.{exc}") from exc
        return cfg


def _offers_yuyv(path: str) -> bool:
    try:
        with v4l2.Device(path) as dev:
            return bool(dev.capability()["capture"]) and "YUYV" in dev.formats()
    except v4l2.V4L2Error:
        return False


def find_device() -> str:
    """The first UVC capture node that offers YUYV, by its stable /dev/v4l/by-id path."""
    candidates = sorted(glob.glob("/dev/v4l/by-id/*-video-index0")) or sorted(
        glob.glob("/dev/video*")
    )
    for path in candidates:
        if _offers_yuyv(path):
            return path
    raise NodeError(
        "no UVC camera with YUYV capture found (is the C920 plugged in?)", 503
    )


class Led:
    """An LED ring switched by a GPIO pin through `pinctrl` (Raspberry Pi OS raspi-utils)."""

    def __init__(
        self,
        gpio: int | None,
        active_high: bool,
        run: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.gpio = gpio
        self.active_high = active_high
        self.state: bool | None = None if gpio is None else False
        self._run = run or (
            lambda cmd: subprocess.run(cmd, check=True, capture_output=True)
        )

    def set(self, on: bool) -> bool:
        if self.gpio is None:
            raise NodeError("no LED configured (led.gpio in the node config)", 404)
        level = "dh" if on == self.active_high else "dl"
        try:
            self._run(["pinctrl", "set", str(self.gpio), "op", level])
        except (OSError, subprocess.CalledProcessError) as exc:
            raise NodeError(
                f"LED on GPIO {self.gpio}: pinctrl failed: {exc}", 500
            ) from exc
        self.state = on
        return on


class Stream:
    """The ustreamer child process serving the MJPEG preview."""

    def __init__(self, cfg: Config, device: str) -> None:
        self.cfg = cfg
        self.device = device
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def available(self) -> bool:
        return (
            self.cfg.stream_enabled and shutil.which(self.cfg.stream_binary) is not None
        )

    def command(self) -> list[str]:
        return [
            self.cfg.stream_binary,
            f"--device={self.device}",
            "--format=MJPEG",
            "--encoder=HW",  # pass the camera's own JPEG frames through: no CPU encoding
            f"--resolution={self.cfg.stream_res}",
            f"--desired-fps={self.cfg.stream_fps}",
            "--host=0.0.0.0",
            f"--port={self.cfg.stream_port}",
            "--persistent",
            "--slowdown",
        ]

    def start(self) -> None:
        if not self.available or self.running:
            return
        self.proc = subprocess.Popen(
            self.command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


@dataclass
class StillRequest:
    width: int
    height: int
    fmt: str
    frames: int
    skip: int
    lock: v4l2.Lock
    led: bool | None
    settle_ms: int
    focus: FocusMode = field(default_factory=lambda: FocusMode("default"))
    focus_roi: tuple[float, float, float, float] = DEFAULT_ROI

    @classmethod
    def from_query(cls, query: Mapping[str, list[str]], cfg: Config) -> StillRequest:
        def one(key: str, default: str | None = None) -> str | None:
            values = query.get(key)
            return values[-1] if values else default

        def integer(key: str, default: int | None) -> int | None:
            raw = one(key)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise NodeError(f"{key} must be an integer (got {raw!r})") from exc

        width, height = parse_res(one("res", cfg.still_res) or cfg.still_res)
        fmt = (one("fmt", "yuyv") or "yuyv").lower()
        if fmt not in FORMATS:
            raise NodeError(f"fmt must be one of {', '.join(FORMATS)} (got {fmt!r})")
        frames = integer("frames", 1) or 1
        if not 1 <= frames <= cfg.max_frames:
            raise NodeError(f"frames must be 1..{cfg.max_frames} (got {frames})")
        skip = integer("skip", cfg.still_skip)
        if skip is None or not 0 <= skip <= MAX_SKIP:
            raise NodeError(f"skip must be 0..{MAX_SKIP}")
        try:
            focus = parse_focus(one("focus") or cfg.lock.get("focus"))
            roi = parse_roi(one("focus_roi"))
        except FocusError as exc:
            raise NodeError(str(exc)) from exc
        locked = {k: integer(k, cfg.lock.get(k)) for k in LOCK_KEYS if k != "focus"}
        extra: list[tuple[str, int]] = []
        for item in query.get("ctrl", []):
            name, sep, value = item.partition("=")
            if not sep or not name:
                raise NodeError(f"ctrl must be NAME=VALUE (got {item!r})")
            try:
                extra.append((name, int(value)))
            except ValueError as exc:
                raise NodeError(
                    f"ctrl {name}: value must be an integer (got {value!r})"
                ) from exc
        led_raw = one("led")
        if led_raw not in (None, "", "on", "off", "keep"):
            raise NodeError(f"led must be on|off|keep (got {led_raw!r})")
        led = {"on": True, "off": False}.get(led_raw or "keep")
        settle = integer("settle_ms", cfg.led_settle_ms) or 0
        if not 0 <= settle <= MAX_SETTLE_MS:
            raise NodeError(f"settle_ms must be 0..{MAX_SETTLE_MS}")
        return cls(
            width,
            height,
            fmt,
            frames,
            skip,
            lock_for(
                focus, locked["exposure"], locked["gain"], locked["wb"], tuple(extra)
            ),
            led,
            settle,
            focus,
            roi,
        )


def lock_for(
    focus: FocusMode,
    exposure: int | None,
    gain: int | None,
    wb: int | None,
    extra: tuple[tuple[str, int], ...] = (),
) -> v4l2.Lock:
    """The Lock for a focus mode: manual N; autofocus on; a sweep starts in manual focus at
    the low end of its range (the sweep then moves it); default leaves focus alone."""
    manual = (
        focus.value
        if focus.mode == "manual"
        else focus.lo
        if focus.mode == "sweep"
        else None
    )
    return v4l2.Lock(manual, exposure, gain, wb, extra, focus_auto=focus.mode == "auto")


def capture_still(
    dev: v4l2.Device,
    width: int,
    height: int,
    frames: int,
    skip: int,
    lock: v4l2.Lock,
    focus: FocusMode,
    roi: tuple[float, float, float, float],
    settle_ms: int = 0,
) -> tuple[v4l2.FrameSet, dict[str, int], dict[str, Any] | None]:
    """Reset → lock → (focus sweep) → capture → read-back → reset to defaults: the camera is
    never left locked. Returns the frames, the read-back during the lock, and the sweep."""
    with v4l2.locked(dev, lock):
        swept = None
        if focus.mode == "sweep":
            sizes = dev.formats().get("YUYV", [])
            res = SWEEP_RES if SWEEP_RES in sizes else (width, height)
            swept = sweep(dev, focus, roi, res)
        if settle_ms:
            time.sleep(settle_ms / 1000)
        shot = dev.capture(width, height, frames, skip)
        readback = {c.name: c.value for c in dev.controls() if c.value is not None}
    return shot, readback, swept


@dataclass
class Still:
    width: int
    height: int
    fmt: str
    frames: list[bytes]
    dropped: int
    controls: dict[str, int]
    seconds: float
    focus: dict[str, Any] = field(
        default_factory=dict
    )  # {"mode", "sweep": {best, scores}|None}

    def body(self) -> bytes:
        if self.fmt == "yuyv":
            return b"".join(self.frames)
        luma = [v4l2.yuyv_luma(f) for f in self.frames]
        if self.fmt == "grey":
            return b"".join(luma)
        header = f"P5\n{self.width} {self.height}\n255\n".encode()
        return b"".join(header + y for y in luma)


class Node:
    """Camera, preview stream and LED behind one lock; the HTTP handler calls into this."""

    def __init__(
        self,
        cfg: Config,
        device: str | None = None,
        led: Led | None = None,
        stream: Stream | None = None,
    ) -> None:
        self.cfg = cfg
        self.device = device or (find_device() if cfg.device == "auto" else cfg.device)
        self.led = led or Led(cfg.led_gpio, cfg.led_active_high)
        self.stream = stream or Stream(cfg, self.device)
        self.lock = threading.Lock()
        self.started = time.time()
        with v4l2.Device(self.device) as dev:
            self.card = dev.capability()["card"]

    # ---- endpoints -------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "name": self.cfg.name,
            "version": VERSION,
            "device": self.device,
            "card": self.card,
            "busy": self.lock.locked(),
            "stream": self.stream.running,
            "stream_port": self.cfg.stream_port if self.stream.available else None,
            "led": self.led.state,
            "max_frames": self.cfg.max_frames,
            "uptime_s": round(time.time() - self.started, 1),
        }

    def controls(self) -> dict[str, Any]:
        with self._camera() as dev:
            return {
                "controls": [c.as_dict() for c in dev.controls()],
                "formats": {k: [list(s) for s in v] for k, v in dev.formats().items()},
            }

    def set_controls(self, values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            ints = {str(k): int(v) for k, v in values.items()}
        except (TypeError, ValueError) as exc:
            raise NodeError(f"control values must be integers: {exc}") from exc
        with self._camera() as dev:
            return {"controls": dev.set_controls(ints)}

    def set_led(self, on: bool) -> dict[str, Any]:
        with self.lock:  # a still sets and restores the LED under the same lock
            return {"led": self.led.set(on)}

    def still(self, req: StillRequest, wait: bool = True) -> Still:
        if not self.lock.acquire(blocking=wait):
            raise NodeError("a still is already being taken", 409)
        try:
            restore_led = self.led.state
            try:
                if req.led is not None:
                    self.led.set(req.led)
                self.stream.stop()
                with v4l2.Device(self.device) as dev:
                    shot, after, swept = capture_still(
                        dev,
                        req.width,
                        req.height,
                        req.frames,
                        req.skip,
                        req.lock,
                        req.focus,
                        req.focus_roi,
                        req.settle_ms if req.led is not None else 0,
                    )
            finally:
                self.stream.start()
                # LED state unknown before the still: off is the safe state to leave it in
                before = False if restore_led is None else restore_led
                if req.led is not None and before != req.led:
                    try:
                        self.led.set(before)
                    except NodeError as exc:  # never mask the capture's own error
                        print(f"topcam: LED restore failed: {exc}", file=sys.stderr)
            return Still(
                req.width,
                req.height,
                req.fmt,
                shot.frames,
                shot.dropped,
                after,
                shot.seconds,
                {"mode": req.focus.text(), "sweep": swept},
            )
        except FocusError as exc:
            raise NodeError(str(exc), 400) from exc
        except v4l2.V4L2Error as exc:
            raise NodeError(str(exc), 503) from exc
        finally:
            self.lock.release()

    @contextlib.contextmanager
    def _camera(self) -> Any:
        """The device for a control call; works while ustreamer streams (controls do not
        need exclusive access) but waits for a running still."""
        with self.lock:
            try:
                with v4l2.Device(self.device) as dev:
                    yield dev
            except v4l2.V4L2Error as exc:
                raise NodeError(str(exc), 400) from exc

    # ---- 3d-api registration -----------------------------------------------------------
    def record(self, own_ip: str) -> dict[str, Any]:
        url = f"http://{own_ip}:{self.cfg.port}"
        stream = (
            f"http://{own_ip}:{self.cfg.stream_port}/stream"
            if self.stream.available
            else None
        )
        try:
            with v4l2.Device(self.device) as dev:
                formats = {k: [list(s) for s in v] for k, v in dev.formats().items()}
        except v4l2.V4L2Error:
            formats = {}
        return {
            "url": url,
            "hostname": f"{socket.gethostname()}.local",
            "stream_url": stream,
            "card": self.card,
            "device": self.device,
            "formats": formats,
            "led": self.cfg.led_gpio is not None,
            "version": VERSION,
        }

    def register_once(
        self, opener: Callable[..., Any] = urllib.request.urlopen
    ) -> bool:
        if not self.cfg.api_url:
            return False
        token = ""
        if self.cfg.api_token_file:
            try:
                with open(self.cfg.api_token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError as exc:
                LOG.warning(
                    "register: cannot read token file %s: %s",
                    self.cfg.api_token_file,
                    exc,
                )
                return False
        target = urllib.parse.urlsplit(self.cfg.api_url)
        body = json.dumps(
            self.record(own_address(target.hostname or "", target.port or 80))
        )
        req = urllib.request.Request(
            f"{self.cfg.api_url.rstrip('/')}/v1/camera-nodes/{urllib.parse.quote(self.cfg.name)}",
            data=body.encode(),
            method="PUT",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with opener(req, timeout=10) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError) as exc:
            LOG.warning("register with %s failed: %s", self.cfg.api_url, exc)
            return False

    def register_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.register_once()
            stop.wait(max(5, self.cfg.api_interval_s))


def own_address(host: str, port: int) -> str:
    """The LAN address other machines reach this node at (the route to the API host)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host or "192.0.2.1", port or 80))
            return str(s.getsockname()[0])
    except OSError:
        return socket.gethostname() + ".local"


def make_handler(node: Node) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"topcam/{VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s %s", self.address_string(), fmt % args)

        def _json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise NodeError("bad Content-Length") from exc
            if not 0 <= length <= 65536:
                raise NodeError("request body length must be 0..65536")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise NodeError(f"body must be JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise NodeError("body must be a JSON object")
            return data

        def _redirect(self, path: str) -> None:
            if not node.stream.available:
                raise NodeError(
                    "the preview stream is off (stream.enabled or no ustreamer)", 404
                )
            host = (self.headers.get("Host") or "localhost").rsplit(":", 1)[0]
            self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
            self.send_header("Location", f"http://{host}:{node.cfg.stream_port}{path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _guard(self, fn: Callable[[], None]) -> None:
            try:
                fn()
            except NodeError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception as exc:  # noqa: BLE001 — a bug answers 500 instead of dropping the socket
                LOG.exception("request failed")
                self._json({"error": f"internal error: {exc}"}, 500)

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True)

            def route() -> None:
                if url.path == "/health":
                    self._json(node.health())
                elif url.path == "/controls":
                    self._json(node.controls())
                elif url.path == "/still":
                    req = StillRequest.from_query(query, node.cfg)
                    shot = node.still(req, wait=query.get("wait", ["1"])[-1] != "0")
                    data = shot.body()
                    self.send_response(200)
                    ctype = (
                        "image/x-portable-graymap"
                        if shot.fmt == "pgm"
                        else "application/octet-stream"
                    )
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Width", str(shot.width))
                    self.send_header("X-Height", str(shot.height))
                    self.send_header("X-Format", shot.fmt)
                    self.send_header("X-Frames", str(len(shot.frames)))
                    self.send_header("X-Dropped", str(shot.dropped))
                    self.send_header("X-Seconds", f"{shot.seconds:.3f}")
                    self.send_header(
                        "X-Controls", json.dumps(shot.controls, separators=(",", ":"))
                    )
                    self.send_header("X-Focus", json.dumps(shot.focus, separators=(",", ":")))
                    self.end_headers()
                    self.wfile.write(data)
                elif url.path in ("/stream", "/snapshot"):
                    self._redirect(url.path)
                else:
                    raise NodeError(f"no route {url.path}", 404)

            self._guard(route)

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path

            def route() -> None:
                if path == "/controls":
                    self._json(node.set_controls(self._body()))
                elif path == "/led":
                    on = self._body().get("on")
                    if not isinstance(on, bool):
                        raise NodeError('body must be {"on": true|false}')
                    self._json(node.set_led(on))
                else:
                    raise NodeError(f"no route {path}", 404)

            self._guard(route)

    return Handler


def load_config(path: str | None, overrides: Mapping[str, Any]) -> Config:
    data: dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Config.from_json(data)


def serve(cfg: Config, bind: str = "0.0.0.0") -> None:
    node = Node(cfg)
    server = ThreadingHTTPServer((bind, cfg.port), make_handler(node))
    stop = threading.Event()
    node.stream.start()
    threading.Thread(target=node.register_forever, args=(stop,), daemon=True).start()

    def shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info(
        "topcam %s: %s (%s) on http://%s:%d",
        VERSION,
        node.device,
        node.card,
        bind,
        cfg.port,
    )
    try:
        server.serve_forever()
    finally:
        node.stream.stop()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="topcam-node", description="top camera node (HTTP)"
    )
    ap.add_argument("--config", help="JSON config file (see the module docstring)")
    ap.add_argument("--name")
    ap.add_argument("--port", type=int)
    ap.add_argument("--device", help="V4L2 node, or auto")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--no-stream", action="store_true", help="no ustreamer preview")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    overrides: dict[str, Any] = {
        "name": args.name,
        "port": args.port,
        "device": args.device,
    }
    try:
        cfg = load_config(args.config, overrides)
    except (OSError, ValueError, NodeError) as exc:
        print(f"topcam-node: bad config: {exc}", file=sys.stderr)
        return 2
    if args.no_stream:
        cfg.stream_enabled = False
    try:
        serve(cfg, args.bind)
    except (NodeError, v4l2.V4L2Error, OSError) as exc:
        print(f"topcam-node: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
