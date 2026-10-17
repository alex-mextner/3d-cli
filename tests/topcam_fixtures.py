"""Fake UVC camera + an in-process topcam node for tests (no /dev/video, no Pi).

FakeCamera models the C920 control set (ids, ranges, autos that make their manual partner
inactive, writes to an inactive control refused like the real driver) and produces YUYV
frames whose luma encodes the frame number, so tests can tell which frames were skipped.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from topcam import node, v4l2

C920_FORMATS = {
    "YUYV": [(640, 480), (1920, 1080), (2304, 1296), (2304, 1536)],
    "MJPG": [(640, 480), (1920, 1080)],
}

# (alias, v4l2-ctl name, label, type, min, max, step, default, the auto alias that disables it)
C920_CONTROLS: list[tuple[str, str, str, int, int, int, int, int, str | None]] = [
    (
        "brightness",
        "brightness",
        "Brightness",
        v4l2.CTRL_TYPE_INTEGER,
        0,
        255,
        1,
        128,
        None,
    ),
    (
        "wb_auto",
        "white_balance_automatic",
        "White Balance, Automatic",
        v4l2.CTRL_TYPE_BOOLEAN,
        0,
        1,
        1,
        1,
        None,
    ),
    ("gain", "gain", "Gain", v4l2.CTRL_TYPE_INTEGER, 0, 255, 1, 0, None),
    (
        "wb",
        "white_balance_temperature",
        "White Balance Temperature",
        v4l2.CTRL_TYPE_INTEGER,
        2000,
        6500,
        1,
        4000,
        "wb_auto",
    ),
    (
        "exposure_auto",
        "auto_exposure",
        "Auto Exposure",
        v4l2.CTRL_TYPE_MENU,
        0,
        3,
        1,
        3,
        None,
    ),
    (
        "exposure",
        "exposure_time_absolute",
        "Exposure Time, Absolute",
        v4l2.CTRL_TYPE_INTEGER,
        3,
        2047,
        1,
        250,
        "exposure_auto",
    ),
    (
        "exposure_auto_priority",
        "exposure_dynamic_framerate",
        "Exposure, Dynamic Framerate",
        v4l2.CTRL_TYPE_BOOLEAN,
        0,
        1,
        1,
        0,
        None,
    ),
    (
        "focus",
        "focus_absolute",
        "Focus, Absolute",
        v4l2.CTRL_TYPE_INTEGER,
        0,
        250,
        5,
        0,
        "focus_auto",
    ),
    (
        "focus_auto",
        "focus_automatic_continuous",
        "Focus, Automatic Continuous",
        v4l2.CTRL_TYPE_BOOLEAN,
        0,
        1,
        1,
        1,
        None,
    ),
]
EXPOSURE_MENU = {1: "Manual Mode", 3: "Aperture Priority Mode"}


@dataclass
class FakeCamera:
    """State shared by every FakeDevice opened on it (one physical camera)."""

    path: str = "/dev/v4l/by-id/usb-fake-C920-video-index0"
    values: dict[str, int] = field(
        default_factory=lambda: {c[0]: c[7] for c in C920_CONTROLS}
    )
    writes: list[tuple[str, int]] = field(default_factory=list)
    captures: list[dict[str, Any]] = field(default_factory=list)
    busy: bool = False
    scene: bytes | None = (
        None  # a YUYV frame every capture returns instead of `frame()`
    )
    focus_target: int | None = (
        None  # stripes whose contrast peaks at this focus position
    )

    def inactive(self, alias: str) -> bool:
        auto = next(c[8] for c in C920_CONTROLS if c[0] == alias)
        if auto is None:
            return False
        on = self.values[auto]
        return on == 3 if auto == "exposure_auto" else on == 1


def stripes(width: int, height: int, focus: int, target: int) -> bytes:
    """Vertical 4-px stripes whose contrast falls off with |focus - target| (a defocus model)."""
    amp = max(0, 100 - 2 * abs(focus - target))
    row = bytearray()
    for x in range(width):
        y = 128 + (amp if (x // 4) % 2 else -amp) // 2
        row += bytes([y, 128])
    return bytes(row) * height


def frame(width: int, height: int, index: int) -> bytes:
    """A YUYV frame: Y = 16 + 10*index everywhere (index 0..23), U = V = 128 (grey)."""
    y = 16 + 10 * index
    return bytes([y, 128, y, 128]) * (width * height // 2)


class FakeDevice(v4l2.Device):
    def __init__(self, cam: FakeCamera, path: str) -> None:  # noqa: D107 — no real open()
        if path != cam.path:
            raise v4l2.V4L2Error(f"{path}: cannot open: No such file or directory")
        self.cam = cam
        self.path = path
        self.fd = -1

    def close(self) -> None:
        pass

    def capability(self) -> dict[str, Any]:
        return {
            "driver": "uvcvideo",
            "card": "HD Pro Webcam C920",
            "bus_info": "usb-fake",
            "capture": True,
        }

    def formats(self) -> dict[str, list[tuple[int, int]]]:
        return {k: list(v) for k, v in C920_FORMATS.items()}

    def controls(self) -> list[v4l2.ControlInfo]:
        out = []
        for alias, name, label, typ, lo, hi, step, default, _ in C920_CONTROLS:
            flags = v4l2.CTRL_FLAG_INACTIVE if self.cam.inactive(alias) else 0
            out.append(
                v4l2.ControlInfo(
                    v4l2.CID[alias],
                    name,
                    label,
                    v4l2._TYPE_NAMES[typ],
                    lo,
                    hi,
                    step,
                    default,
                    flags,
                    self.cam.values[alias],
                    EXPOSURE_MENU if alias == "exposure_auto" else {},
                )
            )
        return out

    def _alias(self, cid: int) -> str:
        return next(a for a, i in v4l2.CID.items() if i == cid)

    def _get(self, cid: int) -> int:
        return self.cam.values[self._alias(cid)]

    def _ioctl(self, request: int, arg: Any, what: str) -> None:
        if request != v4l2.VIDIOC_S_CTRL:
            raise AssertionError(f"unexpected ioctl {what}")
        alias = self._alias(arg.id)
        if self.cam.inactive(alias):  # the real uvcvideo answers EACCES here
            raise v4l2.V4L2Error(f"{self.path}: {what}: Permission denied")
        self.cam.values[alias] = int(arg.value)
        self.cam.writes.append((alias, int(arg.value)))

    def capture(
        self,
        width: int,
        height: int,
        count: int = 1,
        skip: int = 0,
        pixelformat: int = v4l2.YUYV,
        buffers: int = 3,
        timeout: float = 10.0,
    ) -> v4l2.FrameSet:
        if (width, height) not in C920_FORMATS["YUYV"]:
            raise v4l2.V4L2Error(
                f"{self.path}: asked for {width}x{height} YUYV, the driver "
                "offers 640x480 YUYV instead"
            )
        self.cam.captures.append(
            {
                "width": width,
                "height": height,
                "count": count,
                "skip": skip,
                "controls": dict(self.cam.values),
            }
        )
        if self.cam.scene is not None:
            frames = [self.cam.scene] * count
        elif self.cam.focus_target is not None:
            frames = [
                stripes(width, height, self.cam.values["focus"], self.cam.focus_target)
            ] * count
        else:
            frames = [frame(width, height, skip + i) for i in range(count)]
        return v4l2.FrameSet(width, height, "YUYV", frames, 0, 0.01)


def install_fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    """Route every topcam.v4l2.Device(path) to one FakeCamera (wrap in a module fixture)."""
    cam = FakeCamera()
    monkeypatch.setattr(v4l2, "Device", lambda path: FakeDevice(cam, path))
    return cam


class FakeStream(node.Stream):
    def __init__(self, cfg: node.Config, device: str) -> None:
        super().__init__(cfg, device)
        self.events: list[str] = []
        self.on = False

    @property
    def available(self) -> bool:
        return self.cfg.stream_enabled

    @property
    def running(self) -> bool:
        return self.on

    def start(self) -> None:
        if self.available and not self.on:
            self.on = True
            self.events.append("start")

    def stop(self) -> None:
        if self.on:
            self.on = False
            self.events.append("stop")


@contextmanager
def serve_node(
    cam: FakeCamera,
    cfg: node.Config | None = None,
    led_calls: list[list[str]] | None = None,
) -> Iterator[tuple[str, node.Node]]:
    """A real ThreadingHTTPServer on 127.0.0.1:<free port> around a fake camera."""
    cfg = cfg or node.Config(device=cam.path, stream_enabled=False)
    cfg.device = cam.path
    led = node.Led(
        cfg.led_gpio,
        cfg.led_active_high,
        run=(led_calls.append if led_calls is not None else None),
    )
    the_node = node.Node(cfg, led=led, stream=FakeStream(cfg, cam.path))
    the_node.stream.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), node.make_handler(the_node))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", the_node
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
