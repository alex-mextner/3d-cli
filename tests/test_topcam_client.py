"""topcam.client (usb + node backends, camera spec resolution) and topcam.frames."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from topcam_fixtures import FakeCamera, frame, serve_node, install_fake_camera

from topcam import frames, v4l2
from topcam.focus import FocusMode
from topcam.client import CameraError, NodeCamera, StillSpec, UsbCamera, open_camera


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    return install_fake_camera(monkeypatch)

LOCK = v4l2.Lock(focus=40, exposure=300, gain=0, wb=4500, extra=(("brightness", 90),))


def test_usb_backend_locks_and_captures(fake_camera: FakeCamera) -> None:
    cam = UsbCamera(fake_camera.path)
    shot = cam.still(StillSpec(640, 480, frames=3, skip=2, lock=LOCK))
    assert shot.frames == [frame(640, 480, i) for i in (2, 3, 4)]
    assert shot.controls["exposure_time_absolute"] == 300
    assert shot.controls["brightness"] == 90
    assert shot.source == {
        "backend": "usb",
        "device": fake_camera.path,
        "card": "HD Pro Webcam C920",
    }
    with pytest.raises(CameraError, match="no LED ring"):
        cam.still(StillSpec(640, 480, led=True))


def test_usb_backend_reports_a_missing_device(fake_camera: FakeCamera) -> None:
    with pytest.raises(CameraError, match="No such file"):
        UsbCamera("/dev/video99").controls()


def test_node_backend_round_trip_over_http(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        cam = NodeCamera(url)
        shot = cam.still(StillSpec(640, 480, frames=2, skip=1, lock=LOCK))
        assert shot.frames == [frame(640, 480, 1), frame(640, 480, 2)]
        assert shot.controls["focus_absolute"] == 40
        assert shot.source == {
            "backend": "node",
            "url": url,
            "card": "HD Pro Webcam C920",
        }
        assert cam.set_controls({"gain": 5}) == {"gain": 5}
        assert cam.health()["ok"] is True
        with pytest.raises(CameraError, match="HTTP 503: .*offers 640x480"):
            cam.still(StillSpec(123, 45))


def test_node_backend_unreachable() -> None:
    with pytest.raises(CameraError, match="unreachable"):
        NodeCamera("http://127.0.0.1:9", timeout=2).health()


def test_still_spec_query() -> None:
    q = StillSpec(2304, 1296, 4, 5, LOCK, led=True, settle_ms=100, focus=FocusMode("manual", 40)).query()
    assert q == {
        "res": "2304x1296",
        "fmt": "yuyv",
        "frames": 4,
        "skip": 5,
        "focus": "40",
        "exposure": 300,
        "gain": 0,
        "wb": 4500,
        "ctrl": ["brightness=90"],
        "led": "on",
        "settle_ms": 100,
    }


def test_open_camera_specs() -> None:
    assert isinstance(open_camera("usb:/dev/video2"), UsbCamera)
    assert (
        open_camera("http://10.0.0.5:8080").describe()["url"] == "http://10.0.0.5:8080"
    )  # type: ignore[union-attr]
    assert open_camera("10.0.0.5").describe()["url"] == "http://10.0.0.5:8080"
    assert open_camera("topcam.lan:9000").describe()["url"] == "http://topcam.lan:9000"
    assert (
        open_camera("topcam", lookup=lambda n: "http://192.168.0.50:8080").describe()[
            "url"
        ]
        == "http://192.168.0.50:8080"
    )
    assert (
        open_camera("topcam", lookup=lambda n: None).describe()["url"]
        == "http://topcam.local:8080"
    )


def test_yuyv_to_rgb_grey_and_colour() -> None:
    grey = frames.to_rgb(bytes([126, 128, 126, 128]) * 2, 4, 1)
    assert np.allclose(grey, (126 - 16) * 1.164, atol=0.01)
    red_ish = frames.to_rgb(bytes([81, 90, 81, 240]), 2, 1)  # BT.601 red
    assert red_ish[0, 0, 0] > 250 and red_ish[0, 0, 1] < 5 and red_ish[0, 0, 2] < 5
    with pytest.raises(ValueError, match="needs 8 bytes"):
        frames.to_rgb(b"\0" * 6, 2, 2)


def test_average_and_noise() -> None:
    imgs = [np.full((2, 2), v, np.float32) for v in (10, 20, 60)]
    assert frames.average(imgs, "mean")[0, 0] == pytest.approx(30)
    assert frames.average(imgs, "median")[0, 0] == pytest.approx(20)
    assert frames.average(imgs, "none")[0, 0] == pytest.approx(60)
    assert frames.noise(imgs) == pytest.approx(np.std([10, 20, 60]))
    assert frames.noise(imgs[:1]) is None
    with pytest.raises(ValueError, match="average mode"):
        frames.average(imgs, "max")


def _read_png(path: Path) -> tuple[int, int, int, int, bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        ihdr = body if kind == b"IHDR" else ihdr
        idat += body if kind == b"IDAT" else b""
        pos += 12 + length
    w, h, depth, color = struct.unpack(">IIBB", ihdr[:10])
    return w, h, depth, color, zlib.decompress(idat)


def test_write_png_8_and_16_bit(tmp_path: Path) -> None:
    rgb = np.zeros((2, 3, 3))
    rgb[1, 2] = (255, 128, 0)
    w, h, depth, color, raw = _read_png(frames.write_png(tmp_path / "a.png", rgb))
    assert (w, h, depth, color) == (3, 2, 8, 2)
    assert raw[0] == 0 and raw[1 + 3 * 3 + 1 + 6 : 1 + 3 * 3 + 1 + 9] == bytes(
        [255, 128, 0]
    )
    grey = np.array([[0.5, 255.0]])
    w, h, depth, color, raw = _read_png(
        frames.write_png(tmp_path / "g.png", grey, depth=16)
    )
    assert (w, h, depth, color) == (2, 1, 16, 0)
    assert struct.unpack(">HH", raw[1:5]) == (round(0.5 * 257), 65535)
    with pytest.raises(ValueError, match="depth"):
        frames.write_png(tmp_path / "x.png", grey, depth=12)
