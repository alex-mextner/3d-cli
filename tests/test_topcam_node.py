"""topcam.node: the camera node's HTTP API over a fake C920 (real HTTP server, no camera)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from topcam_fixtures import FakeCamera, frame, serve_node, install_fake_camera

from topcam import node


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    return install_fake_camera(monkeypatch)


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _post(url: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_and_controls(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        status, _, body = _get(url + "/health")
        health = json.loads(body)
        assert status == 200
        assert health["ok"] is True and health["card"] == "HD Pro Webcam C920"
        assert health["device"] == fake_camera.path and health["busy"] is False
        status, _, body = _get(url + "/controls")
        data = json.loads(body)
        assert {c["name"] for c in data["controls"]} >= {
            "focus_absolute",
            "auto_exposure",
        }
        assert [2304, 1536] in data["formats"]["YUYV"]


def test_still_locks_controls_then_skips_and_returns_raw_yuyv(
    fake_camera: FakeCamera,
) -> None:
    with serve_node(fake_camera) as (url, _):
        status, headers, body = _get(
            url
            + "/still?res=640x480&frames=2&skip=4&focus=40&exposure=300&gain=0&wb=4500"
            "&ctrl=brightness=100"
        )
    assert status == 200
    assert headers["x-width"] == "640" and headers["x-height"] == "480"
    assert headers["x-format"] == "yuyv" and headers["x-frames"] == "2"
    assert body == frame(640, 480, 4) + frame(640, 480, 5)
    shot = fake_camera.captures[-1]
    assert (shot["count"], shot["skip"]) == (2, 4)
    # the lock was in place BEFORE streaming started
    assert shot["controls"]["focus"] == 40 and shot["controls"]["focus_auto"] == 0
    assert (
        shot["controls"]["exposure_auto"] == 1 and shot["controls"]["exposure"] == 300
    )
    assert shot["controls"]["wb_auto"] == 0 and shot["controls"]["brightness"] == 100
    readback = json.loads(headers["x-controls"])
    assert (
        readback["exposure_time_absolute"] == 300 and readback["focus_absolute"] == 40
    )


def test_still_grey_and_pgm(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        _, _, grey = _get(url + "/still?res=640x480&fmt=grey&skip=0")
        _, headers, pgm = _get(url + "/still?res=640x480&fmt=pgm&frames=2&skip=1")
    assert grey == bytes([16]) * (640 * 480)
    assert headers["content-type"] == "image/x-portable-graymap"
    head = b"P5\n640 480\n255\n"
    assert pgm == head + bytes([26]) * (640 * 480) + head + bytes([36]) * (640 * 480)


def test_config_lock_is_the_default_and_query_overrides_it(
    fake_camera: FakeCamera,
) -> None:
    cfg = node.Config(stream_enabled=False, lock={"focus": 30, "exposure": 200})
    with serve_node(fake_camera, cfg) as (url, _):
        _get(url + "/still?res=640x480")
        assert fake_camera.captures[-1]["controls"]["focus"] == 30
        _get(url + "/still?res=640x480&exposure=150")
        assert fake_camera.captures[-1]["controls"]["exposure"] == 150


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("res=640", "res must be WIDTHxHEIGHT"),
        ("res=640x480&fmt=jpeg", "fmt must be one of yuyv, grey, pgm"),
        ("res=640x480&frames=9", "frames must be 1..8"),
        ("res=640x480&focus=x", "accepted auto | N"),
        ("res=640x480&ctrl=gain", "ctrl must be NAME=VALUE"),
        ("res=640x480&led=blink", "led must be on|off|keep"),
        ("res=640x480&skip=100000", "skip must be 0..120"),
        ("res=640x480&settle_ms=2147483647", "settle_ms must be 0..5000"),
    ],
)
def test_bad_still_requests_are_400(
    fake_camera: FakeCamera, query: str, message: str
) -> None:
    with serve_node(fake_camera) as (url, _):
        status, _, body = _get(url + "/still?" + query)
    assert status == 400
    assert message in json.loads(body)["error"]
    assert fake_camera.captures == []


def test_unsupported_resolution_and_out_of_range_lock_are_503_with_the_driver_reason(
    fake_camera: FakeCamera,
) -> None:
    with serve_node(fake_camera) as (url, _):
        status, _, body = _get(url + "/still?res=123x45")
        assert status == 503 and "offers 640x480" in json.loads(body)["error"]
        status, _, body = _get(url + "/still?res=640x480&gain=999")
        assert (
            status == 503 and "gain=999 is outside 0..255" in json.loads(body)["error"]
        )


def test_still_stops_and_restarts_the_preview_stream(fake_camera: FakeCamera) -> None:
    cfg = node.Config(stream_enabled=True)
    with serve_node(fake_camera, cfg) as (url, the_node):
        _get(url + "/still?res=640x480")
        assert the_node.stream.events == ["start", "stop", "start"]  # type: ignore[attr-defined]
        status, headers, _ = _get(url + "/health")
        assert json.loads(_)["stream"] is True
        req = urllib.request.Request(url + "/stream")
        opener = urllib.request.build_opener(_NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(req, timeout=5)
        assert err.value.code == 307
        assert err.value.headers["Location"] == "http://127.0.0.1:8081/stream"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_stream_off_is_404(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        status, _, body = _get(url + "/stream")
    assert status == 404 and "preview stream is off" in json.loads(body)["error"]


def test_post_controls_sets_and_reads_back(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        status, data = _post(url + "/controls", {"focus_auto": 0, "focus": 55})
        assert status == 200
        assert data == {
            "controls": {"focus_automatic_continuous": 0, "focus_absolute": 55}
        }
        status, data = _post(url + "/controls", {"focus": "x"})
        assert status == 400


def test_led_through_pinctrl_and_restored_after_still(fake_camera: FakeCamera) -> None:
    calls: list[list[str]] = []
    cfg = node.Config(stream_enabled=False, led_gpio=17, led_settle_ms=0)
    with serve_node(fake_camera, cfg, led_calls=calls) as (url, _):
        status, data = _post(url + "/led", {"on": True})
        assert (status, data) == (200, {"led": True})
        _post(url + "/led", {"on": False})
        _get(url + "/still?res=640x480&led=on")
        status, data = _post(url + "/led", {"on": "yes"})
        assert status == 400
    assert calls == [
        ["pinctrl", "set", "17", "op", "dh"],
        ["pinctrl", "set", "17", "op", "dl"],
        ["pinctrl", "set", "17", "op", "dh"],  # on for the still
        ["pinctrl", "set", "17", "op", "dl"],  # restored
    ]


def test_led_active_low_and_missing_led(fake_camera: FakeCamera) -> None:
    calls: list[list[str]] = []
    led = node.Led(4, active_high=False, run=calls.append)
    led.set(True)
    assert calls == [["pinctrl", "set", "4", "op", "dl"]]
    with serve_node(fake_camera) as (url, _):
        status, data = _post(url + "/led", {"on": True})
    assert status == 404 and "no LED configured" in data["error"]


def test_unknown_route_is_404(fake_camera: FakeCamera) -> None:
    with serve_node(fake_camera) as (url, _):
        assert _get(url + "/nope")[0] == 404
        assert _post(url + "/nope", {})[0] == 404


class _Resp:
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return b"{}"


def test_registration_puts_the_record_with_the_bearer_token(
    fake_camera: FakeCamera, tmp_path: Any
) -> None:
    token = tmp_path / "token"
    token.write_text("po_camera_token\n")
    cfg = node.Config(
        name="topcam",
        device=fake_camera.path,
        stream_enabled=False,
        led_gpio=17,
        api_url="http://127.0.0.1:9",
        api_token_file=str(token),
    )
    sent: list[urllib.request.Request] = []

    def opener(req: urllib.request.Request, timeout: float) -> _Resp:
        sent.append(req)
        return _Resp()

    the_node = node.Node(cfg)
    assert the_node.register_once(opener) is True
    req = sent[0]
    assert req.get_method() == "PUT"
    assert req.full_url == "http://127.0.0.1:9/v1/camera-nodes/topcam"
    assert req.headers["Authorization"] == "Bearer po_camera_token"
    body = json.loads(req.data)  # type: ignore[arg-type]
    assert body["url"].endswith(":8080") and body["stream_url"] is None
    assert body["card"] == "HD Pro Webcam C920" and body["led"] is True
    assert body["version"] == node.VERSION and [2304, 1536] in body["formats"]["YUYV"]


def test_registration_without_api_or_with_unreadable_token_does_nothing(
    fake_camera: FakeCamera,
    tmp_path: Any,
) -> None:
    def opener(*_: object, **__: object) -> _Resp:
        raise AssertionError("must not call the API")

    assert (
        node.Node(node.Config(device=fake_camera.path)).register_once(opener) is False
    )
    cfg = node.Config(
        device=fake_camera.path, api_url="http://x", api_token_file=str(tmp_path / "no")
    )
    assert node.Node(cfg).register_once(opener) is False


def test_config_from_json_validates() -> None:
    cfg = node.Config.from_json(
        {
            "name": "topcam",
            "still": {"res": "2304x1296", "lock": {"focus": 30}},
            "led": {"gpio": 17},
            "api": {"url": "http://a:8765"},
        }
    )
    assert (cfg.still_res, cfg.lock, cfg.led_gpio, cfg.api_url) == (
        "2304x1296",
        {"focus": 30},
        17,
        "http://a:8765",
    )
    with pytest.raises(node.NodeError, match="unknown key"):
        node.Config.from_json({"still": {"lock": {"iso": 100}}})
    with pytest.raises(node.NodeError, match="WIDTHxHEIGHT"):
        node.Config.from_json({"stream": {"res": "big"}})
    with pytest.raises(node.NodeError, match="name 'bed cam'"):
        node.Config.from_json({"name": "bed cam"})
    with pytest.raises(node.NodeError, match="sweep"):
        node.Config.from_json({"still": {"lock": {"focus": "near"}}})
    assert node.Config.from_json({"still": {"lock": {"focus": "sweep:0-200"}}}).lock == {"focus": "sweep:0-200"}


def test_main_reports_a_bad_config(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    assert node.main(["--config", str(bad)]) == 2
    assert "bad config" in capsys.readouterr().err


def test_stream_command_passes_camera_jpeg_through() -> None:
    cmd = node.Stream(
        node.Config(stream_res="800x600", stream_fps=3), "/dev/video0"
    ).command()
    assert "--format=MJPEG" in cmd and "--encoder=HW" in cmd
    assert (
        "--resolution=800x600" in cmd
        and "--desired-fps=3" in cmd
        and "--port=8081" in cmd
    )
