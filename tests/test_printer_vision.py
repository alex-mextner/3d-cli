"""`3d printer vision` (issue https://github.com/alex-mextner/3d-cli/issues/150): camera grab and
keepalive, the U1 klippy.log verdict tap, the failure tracker, edge-triggered events, and the
eval/model helpers of lib/vision_detect.py. Only loopback fakes; never a real printer, and no
real model weights (a fake scorer stands in for Obico)."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from errors import ThreeDError
from printer_ops import vision
from printer_ops.base import PrinterCommError
from printer_ops.vision import (
    CRITICAL,
    EOI_TAIL_MAX,
    WARNING,
    FailureTracker,
    Frame,
    FrameScore,
    KlippyLogTap,
    MoonrakerCamera,
    NpuVerdict,
    VisionMonitor,
    is_complete_jpeg,
    jpeg_frame,
    parse_klippy_verdicts,
)

# SOI, APP0 (16-byte segment), SOS (8-byte header), scan data with a stuffed 0xFF, EOI.
JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10" + b"\x00" * 14 + b"\xff\xda\x00\x08" + b"\x00" * 6
    + b"\x12\x34\xff\x00\x56" * 8 + b"\xff\xd9"
)
NOW = datetime(2026, 10, 14, 13, 50, tzinfo=timezone.utc).timestamp()

# verbatim lines from the U1's klippy.log (2026-10-14)
KLIPPY = """\
13:45:25.627:[defect_detection] response info: {'id': 2058, 'jsonrpc': '2.0', 'result': {'err_code': 0, 'noodle': {'obj_areas': 0.0, 'obj_cnts': 0, 'obj_probs': 0.0, 'obj_weighted_probs': 0.0}, 'state': 'success'}}
13:46:05.542:[defect_detection] response info: {'id': 2059, 'jsonrpc': '2.0', 'result': {'err_code': 0, 'noodle': {'obj_areas': 0.2262, 'obj_cnts': 1, 'obj_probs': 0.8989, 'obj_weighted_probs': 0.8989}, 'state': 'success'}}
13:46:05.543:[defect_detection] detected noodle, times: 1
04:38:03.288:Raising exception: id:532 index:0 code:2 oneshot:1 level:2 is_persistent:0, message: detected noodle
"""


# ---- fakes --------------------------------------------------------------------------------------


@dataclass
class FakeCam:
    """Serves /server/files/camera/monitor.jpg from a script of (status, body) replies and
    /server/files/logs/klippy.log with Range support. Records every request."""

    replies: list[tuple[int, bytes]] = field(default_factory=list)
    log: bytes = b""
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)
    port: int = 0

    @contextmanager
    def serve(self) -> Iterator[FakeCam]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(
                self, code: int, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                fake.requests.append(("GET", path, self.headers.get("Range")))
                if path in ("/server/files/camera/monitor.jpg", "/webcam/"):
                    code, body = fake.replies.pop(0) if fake.replies else (200, JPEG)
                    self._send(
                        code, body, {"Last-Modified": "Wed, 14 Oct 2026 13:40:07 GMT"}
                    )
                elif path == "/server/files/logs/klippy.log":
                    self._range(fake.log, self.headers.get("Range") or "")
                else:
                    self._send(404, b"{}")

            def _range(self, data: bytes, rng: str) -> None:
                total = len(data)
                spec = rng.removeprefix("bytes=")
                start = (
                    max(0, total - int(spec[1:]))
                    if spec.startswith("-")
                    else int(spec.split("-")[0] or 0)
                )
                if start >= total:
                    self._send(416, b"", {"Content-Range": f"bytes */{total}"})
                    return
                self._send(
                    206,
                    data[start:],
                    {"Content-Range": f"bytes {start}-{total - 1}/{total}"},
                )

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path, None))
                self._send(405, b"")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield self
        finally:
            srv.shutdown()
            srv.server_close()


# ---- camera -------------------------------------------------------------------------------------


def test_complete_jpeg_needs_soi_and_eoi() -> None:
    assert is_complete_jpeg(JPEG)
    assert not is_complete_jpeg(JPEG[:-2])
    assert not is_complete_jpeg(b"<html>502</html>")


def test_streamer_frame_accepts_encoder_padding_after_eoi() -> None:
    # K1 SE mjpg-streamer: 25 filler bytes after FFD9 (captured 2026-10-15).
    padded = JPEG + b"\x14E\x14\x00Q" * 5
    assert is_complete_jpeg(padded, EOI_TAIL_MAX)
    assert jpeg_frame(padded, EOI_TAIL_MAX) == JPEG  # filler dropped: frozen camera hashes same
    assert not is_complete_jpeg(JPEG + b"\x14" * 200, EOI_TAIL_MAX)
    assert jpeg_frame(JPEG + b"\x14\xff\xd9", EOI_TAIL_MAX) == JPEG  # FFD9 in filler ignored


def test_in_place_file_rejects_a_tail_after_eoi() -> None:
    # A half-rewritten monitor.jpg can end in the old frame's EOI plus leftover bytes.
    assert not is_complete_jpeg(JPEG + b"\x14E\x14\x00Q")
    assert is_complete_jpeg(JPEG + b"\x00" * 2000)  # zero fill is still fine


def test_markers_inside_segment_payloads_do_not_count() -> None:
    # FFD9 in a COM payload of a file cut before its scan.
    assert not is_complete_jpeg(b"\xff\xd8\xff\xfe\x00\x06\xff\xd9\x00\x00", EOI_TAIL_MAX)
    # Fake SOS and EOI both inside a COM payload, no real scan.
    assert not is_complete_jpeg(
        b"\xff\xd8\xff\xfe\x00\x0a\xff\xda\x11\x22\xff\xd9\x33\x44", EOI_TAIL_MAX
    )
    # A truncated scan never reaches EOI.
    assert not is_complete_jpeg(JPEG[:-6], EOI_TAIL_MAX)


def test_grab_from_a_streamer_drops_encoder_padding() -> None:
    with FakeCam(replies=[(200, JPEG + b"\x14E\x14\x00Q" * 5)]).serve() as fake:
        url = f"http://127.0.0.1:{fake.port}/webcam/?action=snapshot"
        frame = MoonrakerCamera("127.0.0.1", fake.port, snapshot=url, retry_delay=0).grab()
    assert frame.jpeg == JPEG


def test_full_u1_monitor_url_is_still_treated_as_an_in_place_file() -> None:
    url = "http://192.168.0.26:7125/server/files/camera/monitor.jpg"
    assert MoonrakerCamera("192.168.0.26", snapshot=url).tail_max == 0
    proxied = "http://printer/moonraker/server/files/camera/monitor.jpg"
    assert MoonrakerCamera("printer", snapshot=proxied).tail_max == 0


def test_tem_marker_in_scan_is_standalone() -> None:
    assert jpeg_frame(JPEG[:-2] + b"\xff\x01" + JPEG[-2:]) == JPEG[:-2] + b"\xff\x01\xff\xd9"


def test_grab_retries_500_and_truncated_frames_then_returns_the_complete_one() -> None:
    with FakeCam(
        replies=[(500, b"FileNotFoundError"), (200, JPEG[:40])]
    ).serve() as fake:
        frame = MoonrakerCamera("127.0.0.1", fake.port, retry_delay=0).grab()
    assert frame.jpeg == JPEG
    assert (
        frame.modified
        == datetime(2026, 10, 14, 13, 40, 7, tzinfo=timezone.utc).timestamp()
    )
    assert [r[0] for r in fake.requests] == ["GET", "GET", "GET"]


def test_grab_gives_up_with_a_structured_error() -> None:
    with FakeCam(replies=[(500, b"x")] * 3).serve() as fake:
        cam = MoonrakerCamera("127.0.0.1", fake.port, retry_delay=0)
        with pytest.raises(PrinterCommError, match="after 3 tries: HTTP 500"):
            cam.grab()


class _FakeWS:
    sent: list[str] = []

    def __enter__(self) -> _FakeWS:
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def send_text(self, text: str) -> None:
        _FakeWS.sent.append(text)


def test_keepalive_sends_only_camera_start_monitor_and_at_most_once_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from printer_ops import ws

    _FakeWS.sent = []
    monkeypatch.setattr(
        ws.WebSocket, "connect", classmethod(lambda cls, *a, **k: _FakeWS())
    )
    cam = MoonrakerCamera("192.0.2.1")
    assert cam.keepalive(1000.0) is True
    assert cam.keepalive(1030.0) is False
    assert cam.keepalive(1061.0) is True
    msgs = [json.loads(s) for s in _FakeWS.sent]
    assert [m["method"] for m in msgs] == [
        "camera.start_monitor",
        "camera.start_monitor",
    ]
    assert msgs[0]["params"] == {"domain": "lan", "interval": 0}


def test_keepalive_is_off_for_a_real_streamer_url() -> None:
    cam = MoonrakerCamera("192.0.2.1", snapshot="http://cam.local/snapshot.jpg")
    assert cam.keepalive_enabled is False
    assert cam.keepalive(1000.0) is False


# ---- the printer's own detector -----------------------------------------------------------------


def test_parse_klippy_verdicts_reads_the_noodle_probabilities() -> None:
    v = parse_klippy_verdicts(KLIPPY, NOW)
    assert [(x.label, x.prob, x.area, x.count) for x in v] == [
        ("noodle", 0.0, 0.0, 0),
        ("noodle", 0.8989, 0.2262, 1),
    ]
    assert (
        datetime.fromtimestamp(v[1].ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        == "2026-10-14 13:46:05"
    )


def test_a_line_from_before_midnight_gets_yesterdays_date() -> None:
    line = "23:59:59.000:[defect_detection] response info: {'result': {'noodle': {'obj_probs': 0.9}}}"
    after_midnight = datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc).timestamp()
    (v,) = parse_klippy_verdicts(line, after_midnight)
    assert datetime.fromtimestamp(v.ts, timezone.utc).day == 14


def test_klippy_tap_tails_then_reads_only_new_lines_and_survives_rotation() -> None:
    filler = b"x" * (vision.TAIL_BYTES + 100) + b"\n"
    with FakeCam(log=filler + KLIPPY.encode()).serve() as fake:
        tap = KlippyLogTap("127.0.0.1", fake.port)
        assert [x.prob for x in tap.poll(NOW)] == [0.0, 0.8989]
        assert tap.poll(NOW) == []  # 416: nothing new
        fake.log += KLIPPY.splitlines()[1].encode()  # a line still being written ...
        assert tap.poll(NOW) == []
        fake.log += b"\n"  # ... and completed
        assert [x.prob for x in tap.poll(NOW)] == [0.8989]
        fake.log = KLIPPY.encode()[:50]  # rotated: shorter than our offset
        assert tap.poll(NOW) == []
        assert tap.offset == 0
    assert {r[0] for r in fake.requests} == {"GET"}


# ---- tracker and monitor ------------------------------------------------------------------------


def test_tracker_stays_quiet_on_healthy_scores_and_escalates_on_spaghetti() -> None:
    t = FailureTracker()
    assert {t.update(p) for p in [0.0, 0.05, 0.1, 0.0] * 50} == {None}
    assert [t.update(2.2) for _ in range(4)] == [None, WARNING, CRITICAL, CRITICAL]


def test_tracker_warmup_ignores_the_first_frames_of_a_print() -> None:
    t = FailureTracker()
    assert [t.update(3.0) for _ in range(6)] == [None] * 6
    assert t.update(3.0) is not None
    t.reset_print()
    assert t.update(3.0) is None


@dataclass
class _Scores:
    values: list[float]
    luma: float = 90.0

    def __call__(self, jpeg: bytes) -> FrameScore:
        return FrameScore(
            self.values.pop(0) if self.values else 0.0,
            [vision.Box(0.1, 0.2, 0.3, 0.4, 0.9)],
            self.luma,
        )


def _frames(n: int, start: float = NOW, step: float = 10.0) -> list[Frame]:
    return [Frame(start + i * step, JPEG + bytes([i % 250]), None) for i in range(n)]


def test_monitor_emits_one_warning_and_one_critical_per_spaghetti_episode(
    tmp_path: Path,
) -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor(
        "u1",
        _Scores([0.0] * 10 + [2.5] * 12 + [0.0] * 40 + [2.5] * 12),
        annotate=lambda j, s: b"annotated",
        evidence_dir=tmp_path,
    )
    mon.set_job("printing", "a.gcode")
    events = [e for f in _frames(74) for e in mon.step(f)[1]]
    assert [(e.kind, e.severity) for e in events] == [
        ("vision_spaghetti", WARNING),
        ("vision_spaghetti", CRITICAL),
    ] * 2
    assert len({e.key for e in events}) == 4
    assert Path(events[0].data["frame"]).read_bytes() == b"annotated"


def test_monitor_is_silent_when_not_printing_and_reports_a_frozen_camera() -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor("u1", _Scores([3.0] * 50))
    mon.set_job("standby", None)
    assert [e for f in _frames(20) for e in mon.step(f)[1]] == []
    mon.set_job("printing", "a.gcode")
    frozen = [Frame(NOW + 300 + i * 10, JPEG, None) for i in range(15)]
    events = [e for f in frozen for e in mon.step(f)[1]]
    assert [e.kind for e in events if e.kind == "vision_camera_stale"] == [
        "vision_camera_stale"
    ]


def test_a_failed_grab_counts_towards_a_stale_camera() -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor("u1", _Scores([]))
    mon.set_job("printing", "a.gcode")
    assert mon.step(_frames(1)[0])[1] == []
    assert mon.step(None, NOW + 60)[1] == []
    assert [e.kind for e in mon.step(None, NOW + 130)[1]] == ["vision_camera_stale"]


def test_monitor_flags_a_dark_frame_once() -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor("u1", _Scores([0.0] * 10, luma=4.0))
    mon.set_job("printing", "a.gcode")
    assert [e.kind for f in _frames(5) for e in mon.step(f)[1]] == [
        "vision_camera_dark"
    ]


def test_monitor_reports_the_printers_own_noodle_verdict_once_per_episode() -> None:
    pytest.importorskip("printer_ops.watch")
    mon = VisionMonitor("u1", _Scores([]))
    mon.set_job("printing", "a.gcode")
    first = mon.npu([NpuVerdict(NOW, "noodle", 0.92, 0.29, 1)], NOW)
    assert [(e.kind, e.data["prob"]) for e in first] == [
        ("printer_noodle_verdict", 0.92)
    ]
    assert mon.npu([NpuVerdict(NOW + 40, "noodle", 0.94, 0.2, 1)], NOW + 40) == []
    assert mon.npu([], NOW + 400) == []  # quiet: episode over
    assert len(mon.npu([NpuVerdict(NOW + 500, "noodle", 0.9, 0.1, 1)], NOW + 500)) == 1
    assert (
        mon.npu([NpuVerdict(NOW + 900, "noodle", 0.5, 0.1, 1)], NOW + 900) == []
    )  # below 0.83


def test_a_new_print_restarts_the_per_print_history_but_a_pause_does_not() -> None:
    mon = VisionMonitor("u1", _Scores([]))
    assert mon.set_job("printing", "a.gcode") is True
    mon.tracker.frames = 50
    assert mon.set_job("paused", "a.gcode") is False
    assert mon.set_job("printing", "a.gcode") is False
    assert mon.tracker.frames == 50
    assert mon.set_job("printing", "b.gcode") is True
    assert mon.tracker.frames == 0
    mon.set_job("complete", "b.gcode")
    assert mon.set_job("printing", "b.gcode") is True  # the same file printed again


# ---- vision_detect helpers ----------------------------------------------------------------------


def test_nms_keeps_the_best_of_overlapping_boxes() -> None:
    np = pytest.importorskip("numpy")
    import vision_detect

    boxes = np.array(
        [[0.1, 0.1, 0.5, 0.5], [0.12, 0.1, 0.5, 0.52], [0.6, 0.6, 0.9, 0.9]], np.float32
    )
    assert vision_detect.nms(boxes, np.array([0.5, 0.9, 0.3], np.float32)) == [1, 2]


def test_metrics_count_frames_and_sequences() -> None:
    import vision_detect

    res = [
        vision_detect.SeqResult("pos", "pos", [0.0, 0.6, 2.0], [None, None, WARNING]),
        vision_detect.SeqResult("neg", "neg", [0.0, 0.25, 0.05], [None, None, None]),
    ]
    m = vision_detect.metrics(res)
    assert m["per_frame"]["0.2"] == {
        "tp": 2,
        "fp": 1,
        "fn": 1,
        "tn": 2,
        "precision": 0.6667,
        "recall": 0.6667,
        "fpr": 0.33333,
    }
    assert m["sequence_alerts"] == {
        "pos_detected": 1,
        "pos_total": 1,
        "neg_false_alarms": 0,
        "neg_total": 1,
    }


def test_ensure_model_verifies_sha256_and_never_keeps_a_bad_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vision_detect

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    good = b"weights"
    spec = vision_detect.ModelSpec(
        "m.onnx",
        "https://example.invalid/m.onnx",
        hashlib.sha256(good).hexdigest(),
        len(good),
        "AGPL-3.0",
        "https://example.invalid",
    )

    def opener(body: bytes) -> Any:
        return lambda url, timeout: io.BytesIO(body)

    with pytest.raises(ThreeDError, match="sha256"):
        vision_detect.ensure_model(spec, opener=opener(b"tampered"))
    assert not list((tmp_path / "3d-cli" / "vision" / "models").iterdir())
    path = vision_detect.ensure_model(spec, opener=opener(good))
    assert path.read_bytes() == good
    assert (
        vision_detect.ensure_model(spec, opener=opener(b"never fetched again")) == path
    )


def test_ensure_model_without_fetch_names_the_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vision_detect

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(ThreeDError, match="spaghetti model"):
        vision_detect.ensure_model(fetch=False)


def test_evaluate_scores_a_directory_in_order(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    import vision_detect

    for i in range(3):
        cv2.imwrite(
            str(tmp_path / f"{i:03d}.png"), np.full((32, 32, 3), i * 60, np.uint8)
        )
    seen: list[int] = []

    def score(img: Any) -> FrameScore:
        seen.append(round(float(img.mean())))
        return FrameScore(0.0)

    starts: list[int] = []
    # the same path twice is still two prints: the scorer is told before each
    res = vision_detect.evaluate(
        [(tmp_path, "neg"), (tmp_path, "neg")],
        score,
        new_print=lambda: starts.append(len(seen)),
    )
    assert [len(r.scores) for r in res] == [3, 3]
    assert seen == [0, 60, 120] * 2
    assert starts == [0, 3]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["u1", "--once"], ["run", "u1", "--once"]),
        (["run", "u1"], ["run", "u1"]),
        (["eval", "--pos", "a"], ["eval", "--pos", "a"]),
        (["fetch-model"], ["fetch-model"]),
        (["record", "u1", "/tmp/rec"], ["record", "u1", "/tmp/rec"]),
    ],
)
def test_printer_vision_routes_to_tool(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: list[str]
) -> None:
    import cli.pyrun
    from commands import printer

    calls: list[tuple[str, str, list[str]]] = []

    def fake_exec(deps: str, script: str, args: list[str]) -> int:
        calls.append((deps, script, args))
        return 0

    monkeypatch.setattr(cli.pyrun, "exec_tool", fake_exec)
    assert printer.run(["vision", *argv]) == 0
    assert calls == [(printer.VISION_DEPS, "vision_detect.py", expected)]


def test_vision_deps_cover_every_third_party_import() -> None:
    """`3d printer vision` runs outside the repo's .venv as `uv run --with VISION_DEPS`, which
    installs nothing else: every third-party module reachable from vision_detect.py (following
    its imports of other lib/ modules) must be listed (0.12.0 shipped without pyyaml, so
    printer-devices.yaml could not be read)."""
    import ast
    import sys as _sys

    from commands import printer

    lib = Path(__file__).resolve().parents[1] / "lib"
    dist = {"cv2": "opencv-python-headless", "yaml": "pyyaml"}
    dev_only = {"tokenizers"}  # vision_verify's prompt regeneration, a dev command

    def local_file(module: str) -> Path | None:
        base = lib.joinpath(*module.split("."))
        for f in (base.with_suffix(".py"), base / "__init__.py"):
            if f.is_file():
                return f
        return None

    needed: set[str] = set()
    todo, seen = [lib / "vision_detect.py"], set()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            for m in mods:
                top = m.split(".")[0]
                if (sub := local_file(m)) is not None:
                    todo.append(sub)
                elif (
                    local_file(top) is None
                    and top not in _sys.stdlib_module_names
                    and top not in dev_only
                ):
                    needed.add(dist.get(top, top))
    assert needed <= set(printer.VISION_DEPS.split(","))


def test_get_turns_http_protocol_errors_into_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    class Conn:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def request(self, *a: Any, **k: Any) -> None: ...
        def getresponse(self) -> Any:
            raise http.client.IncompleteRead(b"\xff\xd8", 100)
        def close(self) -> None: ...

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    with pytest.raises(OSError, match="IncompleteRead"):
        vision._get("http://127.0.0.1:1/x.jpg", 1.0)


def test_ensure_model_refetches_a_corrupted_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vision_detect

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    good = b"weights"
    spec = vision_detect.ModelSpec(
        "m.onnx", "https://example.invalid/m.onnx", hashlib.sha256(good).hexdigest(),
        len(good), "AGPL-3.0", "https://example.invalid",
    )
    models = tmp_path / "3d-cli" / "vision" / "models"
    models.mkdir(parents=True)
    (models / "m.onnx").write_bytes(b"bit-rotted")
    path = vision_detect.ensure_model(spec, opener=lambda url, timeout: io.BytesIO(good))
    assert path.read_bytes() == good
    assert [p.name for p in models.iterdir()] == ["m.onnx"]  # no temp file left behind


def test_malformed_npu_values_are_skipped_not_fatal() -> None:
    text = (
        "13:46:00.000:[defect_detection] response info: {'result': {'noodle': {'obj_probs': 'x'}}}\n"
        "13:46:05.000:[defect_detection] response info: {'result': {'noodle': {'obj_probs': 0.9}}}"
    )
    assert [v.prob for v in parse_klippy_verdicts(text, NOW)] == [0.9]


def test_a_verdict_older_than_a_later_line_is_dated_a_day_earlier() -> None:
    # NOW is 13:50: an old 13:48 line followed by a 13:30 line must be from yesterday.
    text = (
        "13:48:00.000:[defect_detection] response info: {'result': {'noodle': {'obj_probs': 0.9}}}\n"
        "13:30:00.000:[defect_detection] response info: {'result': {'noodle': {'obj_probs': 0.1}}}"
    )
    old, new = parse_klippy_verdicts(text, NOW)
    assert NOW - old.ts > 86000
    assert NOW - new.ts == 20 * 60
